#coding: utf-8
'''
Author: Weiping Song
Contact: songweiping@pku.edu.cn
'''

import argparse
import sys
import time

import numpy as np
import tensorflow as tf

from .model import *
from .sampler import *
from .utils import *

parser = argparse.ArgumentParser(description='Sequential or session-based recommendation')
parser.add_argument('--model', type=str, default='tcn', help='sequential model: rnn/tcn/transformer. (default: tcn)')
parser.add_argument('--batch_size', type=int, default=128, help='batch size (default: 128)')
parser.add_argument('--seq_len', type=int, default=20, help='max sequence length (default: 20)')
parser.add_argument('--dropout', type=float, default=0.2, help='dropout (default: 0.2)')
parser.add_argument('--l2_reg', type=float, default=0.0, help='regularization scale (default: 0.0)')
parser.add_argument('--clip', type=float, default=1., help='gradient clip (default: 1.)')
parser.add_argument('--epochs', type=int, default=20, help='upper epoch limit (default: 20)')
parser.add_argument('--lr', type=float, default=0.001, help='initial learning rate for Adam (default: 0.001)')
parser.add_argument('--emsize', type=int, default=100, help='dimension of item embedding (default: 100)')
parser.add_argument('--neg_size', type=int, default=1, help='size of negative samples (default: 10)')
parser.add_argument('--worker', type=int, default=10, help='number of sampling workers (default: 10)')
parser.add_argument('--nhid', type=int, default=100, help='number of hidden units (default: 100)')
parser.add_argument('--levels', type=int, default=3, help='# of levels (default: 3)')
parser.add_argument('--seed', type=int, default=1111, help='random seed (default: 1111)')
parser.add_argument('--loss', type=str, default='ns', help='type of loss: ns/sampled_sm/full_sm (default: ns)')
parser.add_argument('--data', type=str, default='gowalla', help='data set name (default: gowalla)')
parser.add_argument('--log_interval', type=int, default=1e2, help='log interval (default: 1e2)')
parser.add_argument('--eval_interval', type=int, default=1e3, help='eval/test interval (default: 1e3)')

# ****************************** unique arguments for rnn model. *******************************************************
# None

# ***************************** unique arguemnts for tcn model.
parser.add_argument('--ksize', type=int, default=3, help='kernel size (default: 100)')

# ****************************** unique arguments for transformer model. *************************************************
parser.add_argument('--num_blocks', type=int, default=3, help='num_blocks')
parser.add_argument('--num_heads', type=int, default=2, help='num_heads')
parser.add_argument('--pos_fixed', type=int, default=0, help='trainable positional embedding usually has better performance')


args = parser.parse_args()
tf.set_random_seed(args.seed)

train_data, val_data, test_data, n_items, n_users = data_generator(args)

train_sampler = Sampler(
                    data=train_data, 
                    n_items=n_items, 
                    n_users=n_users,
                    batch_size=args.batch_size, 
                    max_len=args.seq_len,
                    neg_size=args.neg_size,
                    n_workers=args.worker,
                    neg_method='rand')

val_data = prepare_eval_test(val_data, batch_size=100, max_test_len= 20)

checkpoint_dir = '_'.join(['save', args.data, args.model, str(args.lr), str(args.l2_reg), str(args.emsize), str(args.dropout)])

print(args)
print ('#Item: ', n_items)
print ('#User: ', n_users)

model = NeuralSeqRecommender(args, n_items, n_users)

lr = args.lr

def evaluate(source, sess):
    total_hit_k = 0.0
    total_ndcg_k = 0.0
    count = 0.0
    for batch in source:
        feed_dict = {model.inp: batch[1], model.dropout: 0.}
        feed_dict[model.pos] = batch[2]
        hit, ndcg, n_target = sess.run([model.hit_at_k, model.ndcg_at_k, model.num_target], feed_dict=feed_dict)
        count += n_target
        total_hit_k += hit
        total_ndcg_k += ndcg

    val_hit = total_hit_k / count 
    val_ndcg = total_ndcg_k / count

    return [val_hit, val_ndcg]

def main():
    global lr
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    sess = tf.Session(config=config)
    init = tf.global_variables_initializer()
    sess.run(init)
    all_val_hit = [-1]
    early_stop_cn = 0
    step_count = 0
    train_loss_l = 0.
    start_time = time.time()
    print('Start training...')
    try:
        while True:
            cur_batch = train_sampler.next_batch()
            inp = np.array(cur_batch[1])
            feed_dict = {model.inp: inp, model.lr: lr, model.dropout: args.dropout}
            feed_dict[model.pos] = np.array(cur_batch[2])
            feed_dict[model.neg] = np.array(cur_batch[3])
            _, train_loss = sess.run([model.train_op, model.loss], feed_dict=feed_dict)
            train_loss_l += train_loss
            step_count += 1
            if step_count % args.log_interval == 0:
                cur_loss = train_loss_l / args.log_interval
                elapsed = time.time() - start_time
                print('| Totol step {:10d} | lr {:02.5f} | ms/batch {:5.2f} | loss {:5.3f}'.format(
                        step_count, lr, elapsed * 1000 / args.log_interval, cur_loss))
                sys.stdout.flush()
                train_loss_l = 0.
                start_time = time.time()

            if step_count % args.eval_interval == 0:
                val_hit, val_ndcg = evaluate(val_data, sess)
                all_val_hit.append(val_hit)
                print('-' * 90)
                print('| End of step {:10d} | valid hit@20 {:8.5f} | valid ndcg@20 {:8.5f}'.format(
                        step_count, val_hit, val_ndcg))
                print('=' * 90)
                sys.stdout.flush()

                if all_val_hit[-1] <= all_val_hit[-2]:
                    lr /= 2.
                    lr = max(lr, 1e-6)
                    early_stop_cn += 1
                else:
                    early_stop_cn = 0
                    model.saver.save(sess, checkpoint_dir + '/model.ckpt')
                if early_stop_cn == 3:
                    print('Validation hit decreases in three consecutive epochs. Stop Training!')
                    sys.stdout.flush()
                    break
                start_time = time.time()
    except Exception as e:
        print(str(e))
        train_sampler.close()
        exit(1)
    train_sampler.close()
    print('Done')

if __name__ == '__main__':
    if not os.path.exists(checkpoint_dir):
        os.mkdir(checkpoint_dir)
    main()
