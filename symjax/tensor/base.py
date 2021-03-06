import jax
import jax.numpy as jnp
import jax.random as jnr
from jax.random import _is_prng_key
import numpy
import inspect
import copy
from functools import wraps
import re
#from ..base import gradients
#print(base.gradient)
#asdf

def _add_method(cls):
    # important we keep the self inside the function call !
    def decorator(func, name=''):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)
        if name == '':
            setattr(cls, func.__name__, wrapper)
        else:
            setattr(cls, name, wrapper)
        return func # returning func means func can still be used normally
    return decorator


def _args_formatting(args, extra_args, indices):
    """ utility function to be used in the Tensor class to correctly join the
    args and extra_args based on the indices

    Parameters:
    -----------

    args: List

    extra_args: List

    indices: List of binary values
        the indices (one per element) to join args and extra_args in the correct
        order

    """
    output = ()
    arg_iterator = iter(args)
    extra_arg_iterator = iter(extra_args)
    for i in indices:
        if i:
            output += (next(extra_arg_iterator),)
        else:
            output += (next(arg_iterator),)
    return output


def reset(item):
    if isinstance(item, list) or isinstance(item, tuple):
        [reset(i) for i in item]
    elif hasattr(item, 'eval_value'):
        item.eval_value = None
    if hasattr(item, 'kwargs'):
        for i in item.kwargs.values():
            reset(i)


def getroots(item, roots=[]):
    if isinstance(item, list) or isinstance(item, tuple):
        return roots + sum([getroots(i, roots) for i in item], [])
    elif hasattr(item, 'roots'):
        return roots + item.roots
    else:
        return []


def get(item, tracker):
    if isinstance(item, slice) or isinstance(item, numpy.ndarray):
        return item
    elif isinstance(item, list):
        current = [get(i, tracker) for i in item]
        return current
    elif isinstance(item, tuple):
        current = [get(i, tracker) for i in item]
        return tuple(current)
    elif item in tracker:
        return tracker[item]
    elif hasattr(item, 'get'):
        return item.get(tracker)
    else:
        return item


def isvar(item):
    """ check whether an item (possibly a nested list etc) contains a variable
    (any subtype of Tensor) """
    # in case of nested lists/tuples, recursively call the function on it
    if isinstance(item, slice):
        return False
    elif isinstance(item, list) or isinstance(item, tuple):
        return numpy.sum([isvar(value) for value in item])
    # otherwise cheack that it is a subtype of Tensor or a Tracer and not
    # a callable
    else:
        cond1 = isinstance(item, Tensor)
#        cond2 = isinstance(item, jax.interpreters.partial_eval.JaxprTracer)
        cond3 = callable(item)
        return cond1 and not cond3  # (cond1 or cond2) and cond3


class Tensor:
    __array_priority__ = 1000
    def __init__(self, shape, dtype, roots=[], copyof=None):
        self.copyof = copyof
        self.roots = roots
        self._shape = tuple(shape)
        self._dtype = dtype

    def __repr__(self):
        return '(Tensor: shape={}, dtype={})'.format(self.shape, self.dtype)

    def __str__(self):
        return self.__repr__()

    @property
    def shape(self):
        return self._shape

    @property
    def dtype(self):
        return self._dtype

    @property
    def ndim(self):
        return len(self.shape)

    def get(self, tracker=None):
        """ this method implements only the case where the tensor is a copy
            of another one such as a variable etc. In this case we simply refer
            to the original get method. Otherwise, there should never be a call
            of get on a Tensor but always on an Op etc"""
        if self.copyof is not None:
            output = self.copyof.get(tracker)
            tracker[self] = output
            return output

_numpy_signature_re = re.compile(r'^([\w., ]+=)?\s*[\w\.]+\(.*\)$')

def update_numpydoc(docstr, fun, op):
    '''Transforms the numpy docstring to remove references of
       parameters that are supported by the numpy version but not the JAX version'''
  
    #Some numpy functions have an extra tab at the beginning of each line,
    #If this function is one of those we remove this extra tab from all the lines
    if not hasattr(op, '__code__'):
      return docstr
    if docstr[:4] == '    ':
      lines = docstr.split('\n')
      for idx, line in enumerate(lines):
        lines[idx] = line.replace('    ', '', 1)
      docstr = '\n'.join(lines)
  
    begin_idx = docstr.find("Parameters")
    begin_idx = docstr.find("--\n", begin_idx) + 2
    end_idx = docstr.find("Returns", begin_idx)
  
    parameters = docstr[begin_idx:end_idx]
    param_list = parameters.replace('\n    ', '@@').split('\n')
    for idx, p in enumerate(param_list):
      param = p[:p.find(' : ')].split(", ")[0]
      if param not in op.__code__.co_varnames:
        param_list[idx] = ''
    param_list = [param for param in param_list if param != '']
    parameters = '\n'.join(param_list).replace('@@', '\n    ')
    return docstr[:begin_idx + 1] + parameters + docstr[end_idx - 2:]

def jax_wrap(func, insert_default_kwargs=True, doc_func=None):
    if doc_func is None:
        doc_func=func

    @wraps(doc_func)
    def op(*args, seed=None, **kwargs):
        # now we evaluate the shape from the jax built-in function

        # first we check if we are in a random function to be careful
        # with the key
        from . import random
        random_func = func in random._RANDOM_FUNCTIONS

        # we need to remove the static arguments first
        # we first do it for the kwars
        static_kwargs = {}
        var_kwargs = {}
        for name, arg in list(kwargs.items()):
            if not isvar(arg):
                static_kwargs.update({name: arg})
            else:
                var_kwargs.update({name: arg})

        # we need to do the same for the args
        indices = list()
        for i, arg in enumerate(args):
            if not isvar(arg):
                indices.append(1)
            else:
                indices.append(0)
        static_args = [arg for i, arg in zip(indices, args) if i]
        var_args = [arg for i, arg in zip(indices, args) if not i]

        # this is just to get shape and dtype so we do not bother
        # to use the correct seed yet
        if random_func:
            key = jax.random.PRNGKey(0)
            static_args = [key] + static_args
            indices.insert(0, 1)

        # we need to define an abstract function that only takes as input the
        # non-static arguments, internally join them with the static ones
        # and return the output. This is because the jax shape inference
        # functions does not work with static arguments (such as the dimensions
        # of the transpose function)
        def abstract_func(*args, **kwargs):
            all_args = _args_formatting(args, static_args, indices)
            return func(*all_args, **kwargs, **static_kwargs)

        # now we evaluate the shape from the jax built-in function
        tree = jax.eval_shape(abstract_func, *var_args, **var_kwargs)

        # now we determine if it is an Op or a Tuple object based on the
        # infered shape
        if type(tree) == list or type(tree) == tuple:
            shapes = [t.shape for t in tree]
            dtypes = [t.dtype for t in tree]
            return Tuple(
                *args,
                _jax_function=func,
                _shapes=shapes,
                _dtypes=dtypes,
                **kwargs)
        elif random_func:
            shape, dtype = tree.shape, tree.dtype
            return RandomOp(
                *args,
                _jax_function=func,
                _shape=shape,
                _dtype=dtype,
                _seed=seed,
                **kwargs)
        else:
            shape, dtype = tree.shape, tree.dtype
            return Op(*args, _jax_function=func, _shape=shape, _dtype=dtype,
                      **kwargs)

    if not hasattr(func, '__doc__') or func.__doc__ is None:
        return op

    if doc_func is not None:
        sections = func.__doc__.split("\n\n")
    
        signatures = []
        summary = None
        for i in range(len(sections)):
          if _numpy_signature_re.match(sections[i]):
            signatures.append(sections[i])
          else:
            summary = sections[i].strip()
            break
        body = "\n\n".join(signatures + sections[i + 1:])
        body = update_numpydoc(body, func , op)
        desc = "ADDITION"
        docstr = (
            "{summary}\n\nLAX-backend implementation of :func:`{fun}`.\n"
            "{lax_description}Original docstring below.\n\n{body}"
            .format(summary=summary, lax_description=desc,
                    fun=func.__name__, body=body))
    
        op.__name__ = func.__name__
        op.__doc__ = docstr

    return op
















class Op(Tensor):
    """an Op generates a Tensor object obtained from a function"""

    def __init__(self, *args, _jax_function, _shape, _dtype, roots=[], **kwargs):

        # save args and kwargs
        self.kwargs = kwargs
        self.args = args
        self.jax_function = _jax_function

        # set roots
        roots = list(set(getroots(list(kwargs.values())+ list(args)) + roots))

        super().__init__(_shape, _dtype, roots)

    def __repr__(self):
        name = 'Tensor(Op={}, shape={}, dtype={})'
        return name.format(self.jax_function.__name__, self.shape, self.dtype)

    def __str__(self):
        return self.__repr__()

    def get(self, tracker=None):
        if tracker is None:
            tracker = dict()
        elif self in tracker:
            return tracker[self]

        # evaluate the function kwargs as explicit jax arrays
        kwargs = dict()
        for name, var in list(self.kwargs.items()):
            kwargs.update({name: get(var, tracker)})
        args = [get(var, tracker) for var in self.args]
        tracker[self] = self.jax_function(*args, **kwargs)
        return tracker[self]


class RandomOp(Tensor):
    """
    This class creates a :obj:`Tensor` object that given a function (see below)
    and its inputs can be used as a Node in the graph construction. This class
    is specialized to deal with random functions, if the function does not
    take a jax.PRNGKey as first argument, then it should not be used.

    Notes:
        This class is not meant to be used by the user. To create your own
        callable node, see :class:`RandomOp`.

    Args:
        msg (str): Human readable string describing the exception.
        code (:obj:`int`, optional): Error code.

    Attributes:
        msg (str): Human readable string describing the exception.
        code (int): Exception error code.

    Examples:
        >>> node = RandomTensor(jax.random.bernoulli, 0, args=(0.5, (3, 3)))
        >>> print(node)
        (RandomTensor : name=custom, dtype=bool, shape=(3, 3))
        >>> node + 2
        (Tensor, dtype=int32, shape=(3, 3))
    """

    def __init__(self, *args, _jax_function, _shape, _dtype, _seed, **kwargs):

        self.kwargs = kwargs
        self.args = args
        self.jax_function = _jax_function
#        if _seed is None:
#            self.seed = numpy.random.randint(0, 1000000)
#        else:
        self.seed = _seed

        # set roots
        roots = getroots([i for i in kwargs.values()] + list(args))
        roots = list(set(roots)) + [self]

        super().__init__(_shape, _dtype, roots)

    def __repr__(self):
        name = 'RandomTensor(Op={}, shape={}, dtype={})'
        return name.format(self.jax_function.__name__, self.shape, self.dtype)

    def get(self, tracker=None):
        if tracker is None:
            tracker = dict()
        elif self in tracker:
            return tracker[self]
        # argument list
        seed = self.seed or numpy.random.randint(0, 1000000)
        if 'rng' in tracker:
            key = jax.random.PRNGKey(seed + tracker['rng'])
        else:
            key = jax.random.PRNGKey(seed)

        # evaluate the function kwargs as explicit jax arrays
        kwargs = dict()
        for name, var in list(self.kwargs.items()):
            kwargs.update({name: get(var, tracker)})
        args = [get(var, tracker) for var in self.args]
        tracker[self] = self.jax_function(key, *args, **kwargs)
        return tracker[self]

        self.args[0] = key
        return super().get(tracker)


class TupleItem(Tensor):

    def __init__(self, shape, dtype, index, parent, roots, name=''):
        self.parent = parent
        self.index = index
        super().__init__(shape, dtype, roots=roots)

    def get(self, tracker=None):
        return self.parent.get(tracker)[self.index]


class Tuple(tuple):

    def __new__(cls, *args, _jax_function, _shapes, _dtypes, **kwargs):

        roots = list(set(getroots(list(kwargs.values())+ list(args))))

        items = [TupleItem(shape, dtype, i, None, roots=roots)
                 for i, (shape, dtype) in enumerate(zip(_shapes, _dtypes))]
        return super(Tuple, cls).__new__(cls, tuple(items))

    def __init__(self, *args, _jax_function, _shapes, _dtypes, **kwargs):

        self.args = args
        self.kwargs = kwargs
        self.jax_function = _jax_function

#        # set roots
#        roots = list()
#        for value in kwargs.values():
#            if hasattr(value, 'roots'):
#                roots += value.roots
#        for value in args:
#            if hasattr(value, 'roots'):
#                roots += value.roots
#
#        roots = list(set(roots))
#
        # set the parent link with the inside items and set the roots too
        for item in self:
            item.parent = self
#            item.roots = roots

        self.args, self.kwargs = args, kwargs

    def get(self, tracker=None):
        if tracker is None:
            tracker = dict()
        if self in tracker:
            return tracker[self]

        # kwarg dictionnary
        args = list()
        for var in self.args:
            args.append(get(var, tracker))

        kwargs = dict()
        for name, var in self.kwargs.items():
            kwargs.update({name: get(var, tracker)})

        # we add the list object itself into the dictionnary first
        tracker[self] = tuple(self.jax_function(*args, **kwargs))

        # then we add each element of the list into the dictionnary
        for i in range(len(self)):
            tracker[self[i]] = tracker[self][i]

        return tracker[self]


class Variable(Tensor):
    """variable that is a standalone persistent tensor. this tensor
    can be updated and differentiated.

    Parameters:
    -----------

        value_or_fn: array or initializer
            the value given as a numpy array or an initializer which
            takes as input the shape and can be type casted afterward
            via numpy.cast

        shape: tuple (optional)
            the shape of the variable, used only if the value_or_fn is an
            initializer

        dtype: dtype (optional)
            the dtype of the variable, used only if the value_or_fn is an
            initializer

        name: str (optional)
            the name of the variable, there is no test of name duplication

        trainable: bool
            whether the variable is trainable or not. It is set as an
            attribute and can be accessed.
    """

    def __init__(self, tensor, name='', trainable=True):
        
        self.trainable = trainable
        from symjax import get_graph
        self.name = self.generate_name(name)
        if get_graph() is not None:
            get_graph().variables[self.name] = self
        self.tensor = tensor
        self.value = jnp.array(copy.deepcopy(self._get_value()))

        if hasattr(self.value, 'shape'):
            shape = self.value.shape
            dtype = self.value.dtype
        else:
            shape = ()
            dtype = type(value)

        self._shape = shape
        self._dtype = dtype

        super().__init__(shape, dtype, roots=[self])

    def generate_name(self, name):

        if name == '':
            name = 'unnamed'
        from symjax import get_graph
        if get_graph() is None:
            return name
        if name not in get_graph().variables:
            return name

        count = 1
        while True:
            if name + '_' + str(count) in get_graph().variables:
                count += 1
            else:
                break
        return name + '_' + str(count)

    def _get_value(self):
        """ utility function that takes the input and return
            the actual value. It deals with cases where the input
            was a function or not etc
        """

        if isinstance(self.tensor, Tensor):
            return numpy.array(self.tensor.get({}))
        else:
            return self.tensor

    def reset(self):
        """reset the value of the variable based on the initial one, whether
        it was an array or initializer. If it was a random initializer,
        nothing guarantees that the reset will give back the original value
        as opposed to the array case
        """
        self.value = jnp.asarray(self._get_value())

    def assign(self, value):
        """assign a new value ot the variable"""
        self.value = jnp.asarray(value)


    def __repr__(self):
        name = 'Variable(name={}, shape={}, dtype={}, train.={})'
        return name.format(self.name, self.shape, self.dtype, self.trainable)

    def get(self, tracker=None):
        if tracker is None:
            tracker = {}
        if self not in tracker:
            tracker[self] = self.value
        return tracker[self]


class Placeholder(Tensor):
    """placeholder is an input to the computational graph that takes outside
    values. That is, it is an input gate to feed data into a computational
    graph as opposed to for example variables which are living in memory and
    are not fed externally.

    Parameters:
    -----------

        shape: tuple
            the shape of the placeholder

        dtype: dtype
            the dtype of the placeholder

        name: str (optional)
            the name of the variable, there is no test of name duplication
    """

    def __init__(self, shape, dtype, name=''):
        self.name = name
        super().__init__(shape, dtype, roots=[self])

    def __repr__(self):
        return '(Placeholder: ' + self.name + 'dtype=' + str(self.dtype) + \
               ', shape=' + str(self.shape) + ')'

    def get(self, tracker):
        if self not in tracker:
            raise ValueError(' no value given for placeholder {}'.format(self))
        return tracker[self]


def placeholder_like(item, name=''):
    if item is None:
        return None
    elif type(item) == list or type(item) == tuple:
        return type(item)([placeholder_like(i) for i in item])
    else:
        return Placeholder(item.shape, item.dtype, name=name)

def match(l1, l2, output):
    if output is None:
        output = dict()
    if type(l1) == list or type(l1) == tuple:
        for a, b in zip(l1, l2):
            match(a, b, output)
    else:
        output.update({l1: l2})

def symjax_to_jax_fn(func):
    def newfn(*args, fn=func):
        pholders = placeholder_like(args)
        symjax_outputs = fn(*pholders)
        feed_dict = {}
        match(pholders, args, feed_dict)
        if None in feed_dict:
            del feed_dict[None]
        outputs = [o.get(feed_dict) if hasattr(o, 'get') else o for o in symjax_outputs]
        return outputs
    return newfn
