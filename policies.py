import numpy as np
import tensorflow as tf
from baselines.a2c.utils import conv, fc, conv_to_fc
from baselines.common.distributions import make_pdtype
from config import Config

'''
Adapted from baselines.ppo2.policies
'''

def nature_cnn(unscaled_images):
    scaled_images = tf.cast(unscaled_images, tf.float32) / 255.
    activ = tf.nn.relu
    h = activ(conv(scaled_images, 'c1', nf=32, rf=8, stride=4, init_scale=np.sqrt(2)))
    h2 = activ(conv(h, 'c2', nf=64, rf=4, stride=2, init_scale=np.sqrt(2)))
    h3 = activ(conv(h2, 'c3', nf=64, rf=3, stride=1, init_scale=np.sqrt(2)))
    h3 = conv_to_fc(h3)
    return activ(fc(h3, 'fc1', nh=512, init_scale=np.sqrt(2)))

def custom_cnn(unscaled_images):
    scaled_images = tf.cast(unscaled_images, tf.float32) / 255.
    activ = tf.nn.relu
    h = activ(conv(scaled_images, 'c1', nf=32, rf=3, stride=1, pad='SAME', init_scale=np.sqrt(2)))
    h = activ(conv(h, 'c2', nf=32, rf=3, stride=1, pad='SAME', init_scale=np.sqrt(2)))
    h = activ(conv(h, 'c3', nf=64, rf=3, stride=1,  pad='SAME', init_scale=np.sqrt(2)))
    h = activ(conv(h, 'c4', nf=64, rf=3, stride=1,  pad='SAME', init_scale=np.sqrt(2)))
    h = conv_to_fc(h)
    return activ(fc(h, 'fc1', nh=512, init_scale=np.sqrt(2)))

class CnnPolicy(object):

    def __init__(self, sess, ob_shape, ac_space, nbatch, nsteps, scope, reuse=False):
        nh, nw, nc = ob_shape
        ob_shape = (nbatch, nh, nw, nc)
        nact = ac_space.n

        X = tf.placeholder(tf.uint8, ob_shape)
        with tf.variable_scope(scope, reuse=reuse):
            if Config.USE_ATARI_SIZE:
                h = nature_cnn(X)
            else:
                h = custom_cnn(X)
            
            pi = fc(h, 'pi', nact, init_scale=0.01)
            vf = fc(h, 'v', 1)[:,0]

        self.pdtype = make_pdtype(ac_space)
        self.pd = self.pdtype.pdfromflat(pi)

        a0 = self.pd.sample()
        neglogp0 = self.pd.neglogp(a0)
        self.initial_state = None

        def step(ob, *_args, **_kwargs):
            a, v, neglogp = sess.run([a0, vf, neglogp0], {X:ob})
            return a, v, self.initial_state, neglogp

        def value(ob, *_args, **_kwargs):
            return sess.run(vf, {X:ob})

        self.X = X
        self.pi = pi
        self.vf = vf
        self.step = step
        self.value = value