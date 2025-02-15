import sys
import time
import numpy as np
import tensorflow as tf
from tensorflow.contrib import layers
from tensorflow.contrib.rnn import OutputProjectionWrapper
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import rnn_cell_impl as rnn_cell
from tensorflow.python.ops import variable_scope

from model_utils import decoder_fn_lib
import model_utils.ops
from base import BaseTFModel
from model_utils.seq2seq import dynamic_rnn_decoder
from model_utils.ops import gaussian_kld
from model_utils.ops import get_bi_rnn_encode
from model_utils.ops import get_bow
from model_utils.ops import get_rnn_encode
from model_utils.ops import norm_log_liklihood
from model_utils.ops import sample_gaussian


class HRED(BaseTFModel):

    def __init__(self, sess, config, api, log_dir, forward, scope=None):

        self.vocab = api.vocab
        self.rev_vocab = api.rev_vocab
        self.vocab_size = len(self.vocab)

        self.sess = sess
        self.scope = scope
        self.max_utt_len = config.max_utt_len
        self.go_id = self.rev_vocab["<s>"]
        self.eos_id = self.rev_vocab["</s>"]

        self.context_cell_size = config.cxt_cell_size
        self.sent_cell_size = config.sent_cell_size
        self.dec_cell_size = config.dec_cell_size

        with tf.name_scope("io"):
            
            self.input_contexts = tf.placeholder(dtype=tf.int32, shape=(None, None, self.max_utt_len), name="dialog_context")
            self.floors = tf.placeholder(dtype=tf.int32, shape=(None, None), name="floor")
            self.context_lens = tf.placeholder(dtype=tf.int32, shape=(None,), name="context_lens")

            # target response given the dialog context
            self.output_tokens = tf.placeholder(dtype=tf.int32, shape=(None, None), name="output_token")
            self.output_lens = tf.placeholder(dtype=tf.int32, shape=(None,), name="output_lens")

            # optimization related variables
            self.learning_rate = tf.Variable(float(config.init_lr), trainable=False, name="learning_rate")
            self.learning_rate_decay_op = self.learning_rate.assign(tf.multiply(self.learning_rate, config.lr_decay))
            self.global_t = tf.placeholder(dtype=tf.int32, name="global_t")
            self.use_prior = tf.placeholder(dtype=tf.bool, name="use_prior")

        max_dialog_len = array_ops.shape(self.input_contexts)[1]
        max_out_len = array_ops.shape(self.output_tokens)[1]
        batch_size = array_ops.shape(self.input_contexts)[0]

        with variable_scope.variable_scope("wordEmbedding"):
            self.embedding = tf.get_variable("embedding", [self.vocab_size, config.embed_size], dtype=tf.float32)
            embedding_mask = tf.constant([0 if i == 0 else 1 for i in range(self.vocab_size)], dtype=tf.float32, shape=[self.vocab_size, 1])
            embedding = self.embedding * embedding_mask

            input_embedding = embedding_ops.embedding_lookup(embedding, tf.reshape(self.input_contexts, [-1]))
            input_embedding = tf.reshape(input_embedding, [-1, self.max_utt_len, config.embed_size])
            output_embedding = embedding_ops.embedding_lookup(embedding, self.output_tokens)

            if config.sent_type == "bow":
                input_embedding, sent_size = get_bow(input_embedding)
                output_embedding, _ = get_bow(output_embedding)

            elif config.sent_type == "rnn":
                sent_cell = self.get_rnncell("gru", self.sent_cell_size, config.keep_prob, 1)
                input_embedding, sent_size = get_rnn_encode(input_embedding, sent_cell, scope="sent_rnn")
                output_embedding, _ = get_rnn_encode(output_embedding, sent_cell, self.output_lens,
                                                     scope="sent_rnn", reuse=True)
            elif config.sent_type == "bi_rnn":
                fwd_sent_cell = self.get_rnncell("gru", self.sent_cell_size, keep_prob=1.0, num_layer=1)
                bwd_sent_cell = self.get_rnncell("gru", self.sent_cell_size, keep_prob=1.0, num_layer=1)
                input_embedding, sent_size = get_bi_rnn_encode(input_embedding, fwd_sent_cell, bwd_sent_cell, scope="sent_bi_rnn")
                output_embedding, _ = get_bi_rnn_encode(output_embedding, fwd_sent_cell, bwd_sent_cell, self.output_lens, scope="sent_bi_rnn", reuse=True)
            else:
                raise ValueError("Unknown sent_type. Must be one of [bow, rnn, bi_rnn]")

            # reshape input into dialogs
            input_embedding = tf.reshape(input_embedding, [-1, max_dialog_len, sent_size])
            if config.keep_prob < 1.0:
                input_embedding = tf.nn.dropout(input_embedding, config.keep_prob)

            # convert floors into 1 hot
            floor_one_hot = tf.one_hot(tf.reshape(self.floors, [-1]), depth=2, dtype=tf.float32)
            floor_one_hot = tf.reshape(floor_one_hot, [-1, max_dialog_len, 2])

            joint_embedding = tf.concat([input_embedding, floor_one_hot], 2, "joint_embedding")
            # [batch_size, max_dialog_len, dim]

        with variable_scope.variable_scope("contextRNN"):

            enc_cell = self.get_rnncell(config.cell_type, self.context_cell_size, keep_prob=1.0, num_layer=config.num_layer)
            _, enc_last_state = tf.nn.dynamic_rnn(
                enc_cell,
                joint_embedding,
                dtype=tf.float32,
                sequence_length=self.context_lens)

            if config.num_layer > 1:
                if config.cell_type == 'lstm':
                    enc_last_state = [temp.h for temp in enc_last_state]

                enc_last_state = tf.concat(enc_last_state, 1)
            else:
                if config.cell_type == 'lstm':
                    enc_last_state = enc_last_state.h


        cond_list = [enc_last_state]
        cond_embedding = tf.concat(cond_list, 1)

        with variable_scope.variable_scope("generationNetwork"):

            gen_inputs = tf.concat([cond_embedding], 1)
            dec_inputs = gen_inputs
            selected_attribute_embedding = gen_inputs
            # Decoder
            if config.num_layer > 1:
                dec_init_state = []
                for i in range(config.num_layer):
                    temp_init = layers.fully_connected(dec_inputs, self.dec_cell_size, activation_fn=None,
                                                        scope="init_state-%d" % i)
                    if config.cell_type == 'lstm':
                        temp_init = rnn_cell.LSTMStateTuple(temp_init, temp_init)

                    dec_init_state.append(temp_init)

                dec_init_state = tuple(dec_init_state)
            else:
                dec_init_state = layers.fully_connected(dec_inputs, self.dec_cell_size, activation_fn=None,
                                                        scope="init_state")
                if config.cell_type == 'lstm':
                    dec_init_state = rnn_cell.LSTMStateTuple(dec_init_state, dec_init_state)


        with variable_scope.variable_scope("decoder"):
            dec_cell = self.get_rnncell(config.cell_type, self.dec_cell_size, config.keep_prob, config.num_layer)
            dec_cell = OutputProjectionWrapper(dec_cell, self.vocab_size)

            if forward:
                loop_func = decoder_fn_lib.context_decoder_fn_inference(None, dec_init_state, embedding,
                                                                        start_of_sequence_id=self.go_id,
                                                                        end_of_sequence_id=self.eos_id,
                                                                        maximum_length=self.max_utt_len,
                                                                        num_decoder_symbols=self.vocab_size,
                                                                        context_vector=selected_attribute_embedding)
                dec_input_embedding = None
                dec_seq_lens = None
            else:
                loop_func = decoder_fn_lib.context_decoder_fn_train(dec_init_state, selected_attribute_embedding)
                dec_input_embedding = embedding_ops.embedding_lookup(embedding, self.output_tokens)
                dec_input_embedding = dec_input_embedding[:, 0:-1, :]
                dec_seq_lens = self.output_lens - 1

                if config.keep_prob < 1.0:
                    dec_input_embedding = tf.nn.dropout(dec_input_embedding, config.keep_prob)

                # apply word dropping. Set dropped word to 0
                if config.dec_keep_prob < 1.0:
                    keep_mask = tf.less_equal(tf.random_uniform((batch_size, max_out_len-1), minval=0.0, maxval=1.0),
                                              config.dec_keep_prob)
                    keep_mask = tf.expand_dims(tf.to_float(keep_mask), 2)
                    dec_input_embedding = dec_input_embedding * keep_mask
                    dec_input_embedding = tf.reshape(dec_input_embedding, [-1, max_out_len-1, config.embed_size])

            dec_outs, _, final_context_state = dynamic_rnn_decoder(dec_cell, loop_func,
                                                                   inputs=dec_input_embedding,
                                                                   sequence_length=dec_seq_lens)
            if final_context_state is not None:
                final_context_state = final_context_state[:, 0:array_ops.shape(dec_outs)[1]]
                mask = tf.to_int32(tf.sign(tf.reduce_max(dec_outs, axis=2)))
                self.dec_out_words = tf.multiply(tf.reverse(final_context_state, axis=[1]), mask)
            else:
                self.dec_out_words = tf.argmax(dec_outs, 2)

        if not forward:
            with variable_scope.variable_scope("loss"):
                labels = self.output_tokens[:, 1:]
                label_mask = tf.to_float(tf.sign(labels))

                rc_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=dec_outs, labels=labels)
                rc_loss = tf.reduce_sum(rc_loss * label_mask, reduction_indices=1)
                self.avg_rc_loss = tf.reduce_mean(rc_loss)
                # used only for perpliexty calculation. Not used for optimzation
                self.rc_ppl = tf.exp(tf.reduce_sum(rc_loss) / tf.reduce_sum(label_mask))

                
                aug_elbo = self.avg_rc_loss

                tf.summary.scalar("rc_loss", self.avg_rc_loss)
                self.summary_op = tf.summary.merge_all()

            self.optimize(sess, config, aug_elbo, log_dir)

        self.saver = tf.train.Saver(tf.global_variables(), write_version=tf.train.SaverDef.V2)


    def batch_2_feed(self, batch, global_t, use_prior, repeat=1):
        context, context_lens, floors, topics, my_profiles, ot_profiles, outputs, output_lens, output_das = batch
        feed_dict = {
                self.input_contexts: context,
                self.context_lens:context_lens,
                self.floors: floors, 
                self.output_tokens: outputs,
                self.output_lens: output_lens,
                self.use_prior: use_prior}

        if repeat > 1:
            tiled_feed_dict = {}
            for key, val in feed_dict.items():
                if key is self.use_prior:
                    tiled_feed_dict[key] = val
                    continue
                multipliers = [1]*len(val.shape)
                multipliers[0] = repeat
                tiled_feed_dict[key] = np.tile(val, multipliers)
            feed_dict = tiled_feed_dict

        if global_t is not None:
            feed_dict[self.global_t] = global_t

        return feed_dict

    def train(self, global_t, sess, train_feed, update_limit=5000):

        rc_losses = []
        rc_ppls = []
        local_t = 0
        start_time = time.time()
        loss_names =  ["rc_loss", "rc_peplexity"]
        while True:
            batch = train_feed.next_batch()
            if batch is None:
                break
            if update_limit is not None and local_t >= update_limit:
                break
            feed_dict = self.batch_2_feed(batch, global_t, use_prior=False)

            _, sum_op, rc_loss, rc_ppl = sess.run(
                [self.train_ops, self.summary_op, self.avg_rc_loss, self.rc_ppl], feed_dict)

            self.train_summary_writer.add_summary(sum_op, global_t)
            
            rc_ppls.append(rc_ppl)
            rc_losses.append(rc_loss)


            global_t += 1
            local_t += 1
            if local_t % (train_feed.num_batch / 20) == 0:
                self.print_loss("%.2f" % (train_feed.ptr / float(train_feed.num_batch)), loss_names, [rc_losses, rc_ppls], "")

        # finish epoch!
        epoch_time = time.time() - start_time
        avg_losses = self.print_loss("Epoch Done", loss_names,
                                     [rc_losses, rc_ppls],
                                     "step time %.4f" % (epoch_time / train_feed.num_batch))

        return global_t, avg_losses[0]

    def valid(self, name, sess, valid_feed):

        rc_losses = []
        rc_ppls = []

        while True:
            batch = valid_feed.next_batch()
            if batch is None:
                break
            feed_dict = self.batch_2_feed(batch, None, use_prior=False, repeat=1)

            rc_loss, rc_ppl = sess.run(
                [self.avg_rc_loss, self.rc_ppl], feed_dict)

            rc_losses.append(rc_loss)
            rc_ppls.append(rc_ppl)

        avg_losses = self.print_loss(name, ["rc_loss", "rc_peplexity"],
                                     [rc_losses, rc_ppls], "")
        return avg_losses[0]


    def test(self, sess, test_feed, num_batch=None, repeat=5, dest=sys.stdout):
        local_t = 0
        # recall_bleus = []
        # prec_bleus = []

        while True:
            batch = test_feed.next_batch()
            if batch is None or (num_batch is not None and local_t > num_batch):
                break
            feed_dict = self.batch_2_feed(batch, None, use_prior=True, repeat=repeat)
            word_outs = sess.run(self.dec_out_words, feed_dict)
            sample_words = np.split(word_outs, repeat, axis=0)
            
            true_floor = feed_dict[self.floors]
            true_srcs = feed_dict[self.input_contexts]
            true_src_lens = feed_dict[self.context_lens]
            true_outs = feed_dict[self.output_tokens]
            local_t += 1

            if dest != sys.stdout:
                if local_t % (test_feed.num_batch / 10) == 0:
                    print("%.2f >> " % (test_feed.ptr / float(test_feed.num_batch))),

            for b_id in range(test_feed.batch_size):
                # print the dialog context
                dest.write("Batch %d index %d\n" % (local_t, b_id))
                start = np.maximum(0, true_src_lens[b_id]-5)
                for t_id in range(start, true_srcs.shape[1], 1):
                    src_str = " ".join([self.vocab[e] for e in true_srcs[b_id, t_id].tolist() if e != 0])
                    dest.write("Src %d-%d: %s\n" % (t_id, true_floor[b_id, t_id], src_str))
                
                # print the true outputs
                true_tokens = [self.vocab[e] for e in true_outs[b_id].tolist() if e not in [0, self.eos_id, self.go_id]]
                true_str = " ".join(true_tokens).replace(" ' ", "'")
                dest.write("Target >> %s\n" % (true_str))

                # print the predicted outputs
                local_tokens = []
                for r_id in range(repeat):
                    pred_outs = sample_words[r_id]
                    pred_tokens = [self.vocab[e] for e in pred_outs[b_id].tolist() if e != self.eos_id and e != 0]
                    pred_str = " ".join(pred_tokens).replace(" ' ", "'")
                    dest.write("Sample %d >> %s\n" % (r_id, pred_str))
                    local_tokens.append(pred_tokens)

                #max_bleu, avg_bleu = utils.get_bleu_stats(true_tokens, local_tokens)
                #recall_bleus.append(max_bleu)
                #prec_bleus.append(avg_bleu)
                # make a new line for better readability
                dest.write("\n")

        #avg_recall_bleu = float(np.mean(recall_bleus))
        #avg_prec_bleu = float(np.mean(prec_bleus))
        #avg_f1 = 2*(avg_prec_bleu*avg_recall_bleu) / (avg_prec_bleu+avg_recall_bleu+10e-12)
        #report = "Avg recall BLEU %f, avg precision BLEU %f and F1 %f (only 1 reference response. Not final result)" \
        #        % (avg_recall_bleu, avg_prec_bleu, avg_f1)
        #print report
        #dest.write(report + "\n")
        print("Done testing")
