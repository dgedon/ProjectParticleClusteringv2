import argparse
import math
import subprocess
from datetime import datetime
import numpy as np
import tensorflow as tf
import socket
import importlib
import os, ast
import sys
from sklearn.cluster import KMeans
import h5py

np.set_printoptions(edgeitems=1000)

from scipy.optimize import linear_sum_assignment

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(BASE_DIR, '..', 'models'))
sys.path.append(os.path.join(BASE_DIR, '..', 'utils'))
import provider
import gapnet_classify as MODEL

parser = argparse.ArgumentParser()

parser.add_argument('--max_dim', type=int, default=3, help='Dimension of the encoding layer [Default: 3]')
parser.add_argument('--n_clusters', type=int, default=3, help='Number of clusters [Default: 3]')
parser.add_argument('--gpu', type=int, default=0, help='GPU to use [default: GPU 0]')
parser.add_argument('--model', default='gapnet_clasify', help='Model name [default: gapnet_classify]')
parser.add_argument('--log_dir', default='log', help='Log dir [default: log]')
parser.add_argument('--num_point', type=int, default=100, help='Point Number [default: 100]')
parser.add_argument('--max_epoch', type=int, default=200, help='Epoch to run [default: 200]')
parser.add_argument('--batch_size', type=int, default=512, help='Batch Size during training [default: 512]')
parser.add_argument('--learning_rate', type=float, default=0.001, help='Initial learning rate [default: 0.01]')

parser.add_argument('--momentum', type=float, default=0.9, help='Initial momentum [default: 0.9]')
parser.add_argument('--optimizer', default='adam', help='adam or momentum [default: adam]')
parser.add_argument('--decay_step', type=int, default=500000, help='Decay step for lr decay [default: 500000]')
parser.add_argument('--wd', type=float, default=0.0, help='Weight Decay [Default: 0.0]')
parser.add_argument('--decay_rate', type=float, default=0.5, help='Decay rate for lr decay [default: 0.5]')
parser.add_argument('--output_dir', type=str, default='train_results',
                    help='Directory that stores all training logs and trained models')
parser.add_argument('--data_dir', default=os.getcwd() + '/../h5', help='directory with data [default: hdf5_data]')
parser.add_argument('--nfeat', type=int, default=8, help='Number of features [default: 8]')
parser.add_argument('--ncat', type=int, default=20, help='Number of categories [default: 20]')

FLAGS = parser.parse_args()
H5_DIR = FLAGS.data_dir

EPOCH_CNT = 0
MAX_PRETRAIN = 10
BATCH_SIZE = FLAGS.batch_size
NUM_POINT = FLAGS.num_point
NUM_FEAT = FLAGS.nfeat
NUM_CLASSES = FLAGS.ncat
MAX_EPOCH = FLAGS.max_epoch
BASE_LEARNING_RATE = FLAGS.learning_rate
GPU_INDEX = FLAGS.gpu
MOMENTUM = FLAGS.momentum
OPTIMIZER = FLAGS.optimizer
DECAY_STEP = FLAGS.decay_step
DECAY_RATE = FLAGS.decay_rate

# MODEL = importlib.import_module(FLAGS.model) # import network module
MODEL_FILE = os.path.join(BASE_DIR, 'models', FLAGS.model + '.py')
LOG_DIR = os.path.join('..', 'logs', FLAGS.log_dir)

if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
os.system('cp %s.py %s' % (MODEL_FILE, LOG_DIR))  # bkp of model def
os.system('cp train_kmeans.py %s' % (LOG_DIR))  # bkp of train procedure
LOG_FOUT = open(os.path.join(LOG_DIR, 'log_dkm.txt'), 'w')
LOG_FOUT.write(str(FLAGS) + '\n')

BN_INIT_DECAY = 0.5
BN_DECAY_DECAY_RATE = 0.5
BN_DECAY_DECAY_STEP = float(DECAY_STEP)
BN_DECAY_CLIP = 0.99

LEARNING_RATE_CLIP = 1e-5
HOSTNAME = socket.gethostname()

name = 'wztop'  # 'multi'  #
TRAIN_FILES = provider.getDataFiles(os.path.join(H5_DIR, 'train_files_' + name + '.txt'))
TEST_FILES = provider.getDataFiles(os.path.join(H5_DIR, 'test_files_' + name + '.txt'))


def log_string(out_str):
    LOG_FOUT.write(out_str + '\n')
    LOG_FOUT.flush()
    print(out_str)


def get_learning_rate(batch):
    learning_rate = tf.compat.v1.train.exponential_decay(
        BASE_LEARNING_RATE,  # Base learning rate.
        batch * BATCH_SIZE,  # Current index into the dataset.
        DECAY_STEP,  # Decay step.
        DECAY_RATE,  # Decay rate.
        staircase=True)
    learning_rate = tf.maximum(learning_rate, LEARNING_RATE_CLIP)  # CLIP THE LEARNING RATE!
    return learning_rate


def get_bn_decay(batch):
    bn_momentum = tf.compat.v1.train.exponential_decay(
        BN_INIT_DECAY,
        batch * BATCH_SIZE,
        BN_DECAY_DECAY_STEP,
        BN_DECAY_DECAY_RATE,
        staircase=True)
    bn_decay = tf.minimum(BN_DECAY_CLIP, 1 - bn_momentum)
    return bn_decay


def train():
    with tf.Graph().as_default():
        with tf.device('/gpu:' + str(GPU_INDEX)):
            pointclouds_pl, labels_pl = MODEL.placeholder_inputs(BATCH_SIZE, NUM_POINT, NUM_FEAT)

            is_training_pl = tf.compat.v1.placeholder(tf.bool, shape=())

            # Note the global_step=batch parameter to minimize. 
            # That tells the optimizer to helpfully increment the 'batch' parameter for you every time it trains.
            batch = tf.Variable(0)
            alpha = tf.compat.v1.placeholder(dtype=tf.float32, shape=())
            bn_decay = get_bn_decay(batch)
            tf.compat.v1.summary.scalar('bn_decay', bn_decay)
            print("--- Get model and loss")

            pred, max_pool = MODEL.get_model(pointclouds_pl, is_training=is_training_pl,
                                             bn_decay=bn_decay,
                                             num_class=NUM_CLASSES, weight_decay=FLAGS.wd,
                                             )

            class_loss = MODEL.get_focal_loss(pred, labels_pl, NUM_CLASSES)
            mu = tf.Variable(tf.zeros(shape=(FLAGS.n_clusters, FLAGS.max_dim)), name="mu",
                             trainable=True)  # k centroids
            kmeans_loss, stack_dist = MODEL.get_loss_kmeans(max_pool, mu, FLAGS.max_dim,
                                                            FLAGS.n_clusters, alpha)

            full_loss = kmeans_loss + class_loss

            print("--- Get training operator")
            # Get training operator
            learning_rate = get_learning_rate(batch)
            tf.compat.v1.summary.scalar('learning_rate', learning_rate)
            if OPTIMIZER == 'momentum':
                optimizer = tf.compat.v1.train.MomentumOptimizer(learning_rate, momentum=MOMENTUM)
            elif OPTIMIZER == 'adam':
                optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate)

            train_op_full = optimizer.minimize(full_loss, global_step=batch)
            train_op = optimizer.minimize(class_loss, global_step=batch)

            # Add ops to save and restore all the variables.
            saver = tf.compat.v1.train.Saver()

        # Create a session
        config = tf.compat.v1.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.compat.v1.Session(config=config)

        sess.run(tf.compat.v1.global_variables_initializer())

        # Add summary writers
        merged = tf.compat.v1.summary.merge_all()
        train_writer = tf.compat.v1.summary.FileWriter(os.path.join(LOG_DIR, 'train'), sess.graph)
        test_writer = tf.compat.v1.summary.FileWriter(os.path.join(LOG_DIR, 'test'), sess.graph)

        # Init variables
        print("Total number of weights for the model: ",
              np.sum([np.prod(v.get_shape().as_list()) for v in tf.compat.v1.trainable_variables()]))
        ops = {'pointclouds_pl': pointclouds_pl,
               'labels_pl': labels_pl,
               'is_training_pl': is_training_pl,
               'max_pool': max_pool,
               'pred': pred,
               'alpha': alpha,
               'mu': mu,
               'stack_dist': stack_dist,
               'class_loss': class_loss,
               'kmeans_loss': kmeans_loss,
               'train_op': train_op,
               'train_op_full': train_op_full,
               'merged': merged,
               'step': batch,
               'learning_rate': learning_rate
               }

        for epoch in range(MAX_EPOCH):
            log_string('**** EPOCH %03d ****' % (epoch))
            sys.stdout.flush()

            is_full_training = epoch > MAX_PRETRAIN
            max_pool = train_one_epoch(sess, ops, train_writer, is_full_training)
            if epoch == MAX_PRETRAIN:
                centers = KMeans(n_clusters=FLAGS.n_clusters).fit(np.squeeze(max_pool))
                centers = centers.cluster_centers_
                sess.run(tf.compat.v1.assign(mu, centers))
            eval_one_epoch(sess, ops, test_writer, is_full_training)
            if is_full_training:
                save_path = saver.save(sess, os.path.join(LOG_DIR, 'cluster.ckpt'))
            else:
                save_path = saver.save(sess, os.path.join(LOG_DIR, 'model.ckpt'))

            log_string("Model saved in file: %s" % save_path)


def get_batch(data, label, start_idx, end_idx):
    batch_label = label[start_idx:end_idx]
    batch_data = data[start_idx:end_idx, :, :]
    return batch_data, batch_label


def cluster_acc(y_true, y_pred):
    """
    Calculate clustering accuracy. Require scikit-learn installed
    """
    y_true = y_true.astype(np.int64)
    D = max(y_pred.max(), y_true.max()) + 1
    w = np.zeros((D, D), dtype=np.int64)
    for i in range(y_pred.size):
        w[y_pred[i], y_true[i]] += 1
    ind = linear_sum_assignment(w.max() - w)
    ind = np.asarray(ind)
    ind = np.transpose(ind)
    return sum([w[i, j] for i, j in ind]) * 1.0 / y_pred.size


def train_one_epoch(sess, ops, train_writer, is_full_training):
    """ ops: dict mapping from string to tf ops """
    is_training = True

    train_idxs = np.arange(0, len(TRAIN_FILES))

    acc = loss_sum = 0
    y_pool = []
    for fn in range(len(TRAIN_FILES)):
        # log_string('----' + str(fn) + '-----')
        current_file = os.path.join(H5_DIR, TRAIN_FILES[train_idxs[fn]])
        current_data, current_label, current_cluster = provider.load_h5_data_label_seg(current_file)

        current_label = np.squeeze(current_label)

        file_size = current_data.shape[0]
        num_batches = file_size // BATCH_SIZE
        # num_batches = 5
        log_string(str(datetime.now()))

        for batch_idx in range(num_batches):
            start_idx = batch_idx * BATCH_SIZE
            end_idx = (batch_idx + 1) * BATCH_SIZE
            batch_data, batch_label = get_batch(current_data, current_label, start_idx, end_idx)
            cur_batch_size = end_idx - start_idx

            # print(batch_weight)
            feed_dict = {ops['pointclouds_pl']: batch_data,
                         ops['labels_pl']: batch_label,
                         ops['is_training_pl']: is_training,
                         ops['alpha']: 2 * (EPOCH_CNT - MAX_PRETRAIN + 1),

                         }
            if is_full_training:
                summary, step, _, loss_val, dist, lr = sess.run([ops['merged'], ops['step'],
                                                                 ops['train_op_full'], ops['kmeans_loss'],
                                                                 ops['stack_dist'], ops['learning_rate']
                                                                 ],
                                                                feed_dict=feed_dict)

                batch_cluster = np.array([np.where(r == 1)[0][0] for r in current_cluster[start_idx:end_idx]])
                cluster_assign = np.zeros((cur_batch_size), dtype=int)

                for i in range(cur_batch_size):
                    index_closest_cluster = np.argmin(dist[:, i])
                    cluster_assign[i] = index_closest_cluster

                acc += cluster_acc(batch_cluster, cluster_assign)
            else:
                summary, step, _, loss_val, max_pool, lr = sess.run([ops['merged'], ops['step'],
                                                                     ops['train_op'], ops['class_loss'],
                                                                     ops['max_pool'], ops['learning_rate']],

                                                                    feed_dict=feed_dict)

                if len(y_pool) == 0:
                    y_pool = np.squeeze(max_pool)
                else:
                    y_pool = np.concatenate((y_pool, np.squeeze(max_pool)), axis=0)

            loss_sum += np.mean(loss_val)

            train_writer.add_summary(summary, step)
    log_string('learning rate: %f' % (lr))
    log_string('train mean loss: %f' % (loss_sum / float(num_batches)))
    log_string('train clustering accuracy: %f' % (acc / float(num_batches)))
    return y_pool


def eval_one_epoch(sess, ops, test_writer, is_full_training):
    """ ops: dict mapping from string to tf ops """
    global EPOCH_CNT
    is_training = False
    test_idxs = np.arange(0, len(TEST_FILES))
    # Test on all data: last batch might be smaller than BATCH_SIZE
    loss_sum = acc = 0
    acc_kmeans = 0

    for fn in range(len(TEST_FILES)):
        # log_string('----' + str(fn) + '-----')
        current_file = os.path.join(H5_DIR, TEST_FILES[test_idxs[fn]])
        current_data, current_label, current_cluster = provider.load_h5_data_label_seg(current_file)
        current_label = np.squeeze(current_label)

        file_size = current_data.shape[0]
        num_batches = file_size // BATCH_SIZE
        # num_batches = 5
        for batch_idx in range(num_batches):
            start_idx = batch_idx * BATCH_SIZE
            end_idx = (batch_idx + 1) * BATCH_SIZE
            batch_data, batch_label = get_batch(current_data, current_label, start_idx, end_idx)
            cur_batch_size = end_idx - start_idx

            feed_dict = {ops['pointclouds_pl']: batch_data,
                         ops['is_training_pl']: is_training,
                         ops['labels_pl']: batch_label,
                         ops['alpha']: 2 * (EPOCH_CNT - MAX_PRETRAIN + 1),

                         }

            if is_full_training:
                summary, step, loss_val, max_pool, dist, mu = sess.run([ops['merged'], ops['step'],
                                                                        ops['kmeans_loss'],
                                                                        ops['max_pool'], ops['stack_dist'],
                                                                        ops['mu']
                                                                        ],
                                                                       feed_dict=feed_dict)
                if batch_idx == 0:
                    log_string("mu: {}".format(mu))
                batch_cluster = np.array([np.where(r == 1)[0][0] for r in current_cluster[start_idx:end_idx]])
                cluster_assign = np.zeros((cur_batch_size), dtype=int)
                for i in range(cur_batch_size):
                    index_closest_cluster = np.argmin(dist[:, i])
                    cluster_assign[i] = index_closest_cluster

                acc += cluster_acc(batch_cluster, cluster_assign)


            else:
                summary, step, loss_val = sess.run([ops['merged'], ops['step'],
                                                    ops['class_loss']
                                                    ],
                                                   feed_dict=feed_dict)

            test_writer.add_summary(summary, step)

            loss_sum += np.mean(loss_val)

    total_loss = loss_sum * 1.0 / float(num_batches)
    log_string('test mean loss: %f' % (total_loss))
    log_string('testing clustering accuracy: %f' % (acc / float(num_batches)))

    EPOCH_CNT += 1


if __name__ == "__main__":
    log_string('pid: %s' % (str(os.getpid())))
    train()
    LOG_FOUT.close()
