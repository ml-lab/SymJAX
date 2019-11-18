import jax
import numpy as np
import sys
sys.path.insert(0, "../")

import theanoxla
import theanoxla.tensor as T

SHAPE = (4, 4)
z = T.Variable(np.random.randn(*SHAPE).astype('float32'), name='z')
w = T.Placeholder(SHAPE, 'float32', name='w')
noise = T.random.uniform(SHAPE, dtype='float32')
y = T.cos(theanoxla.nn.activations.leaky_relu(z,0.3) + w + noise)
cost = T.pool(y, (2, 2))
cost = T.sum(cost)

grads = theanoxla.gradients(cost, [w, z], [1])

print(cost.get({w: np.random.randn(*SHAPE)}))
noise.seed = 20
print(cost.get({w: np.random.randn(*SHAPE)}))
noise.seed = 40
print(cost.get({w: np.random.randn(*SHAPE)}))

updates = {z:z-0.01*grads[0]}
fn1 = theanoxla.function(w, outputs=[cost])
fn2 = theanoxla.function(w, outputs=[cost], updates=updates)
print(fn1(np.random.randn(*SHAPE)))
print(fn1(np.random.randn(*SHAPE)))

cost = list()
for i in range(1000):
    cost.append(fn2(np.ones(SHAPE))[0])

import matplotlib.pyplot as plt
plt.plot(cost)
plt.show()