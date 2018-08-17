import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops
from tensorflow.contrib.distributions import Bernoulli


class Generator:
    def __init__(self, num_emb, batch_size, emb_dim, hidden_dim,
                 sequence_length, start_token, table, mode='train',
                 learning_rate=0.01, reward_gamma=0.95, has_input=False):
        assert mode.lower() in ['train', 'infer']
        self.num_emb = num_emb
        self.batch_size = batch_size
        self.emb_dim = emb_dim
        self.hidden_dim = hidden_dim
        self.sequence_length = sequence_length
        self.start_token = tf.constant([start_token] * self.batch_size, dtype=tf.int32)
        self.learning_rate = tf.Variable(float(learning_rate), trainable=False, name="lr")
        self.reward_gamma = reward_gamma
        self.g_params = []
        self.d_params = []
        self.temperature = 1.0
        self.grad_clip = 5.0
        self.mode = mode.lower()

        self.expected_reward = tf.Variable(tf.zeros([sequence_length]))
        self.table = table

        # LSTM generator
        with tf.variable_scope("generator"):
            self.g_embeddings = tf.Variable(tf.random_normal([self.num_emb, self.emb_dim]), name="embedding"
                                            )
            self.g_params.append(self.g_embeddings)
            self.g_recurrent_unit = self.lstm_unit(self.g_params)  # maps h_tm1 to h_t for generator
            self.g_output_unit = self.output_unit(self.g_params)  # maps h_t to o_t (output token logits)

        # placeholder
        self.x = tf.placeholder(tf.int32, shape=[self.batch_size,
                                                 self.sequence_length])  # sequence of tokens generated by generator

        self.rewards = tf.placeholder(tf.float32, shape=[self.batch_size,
                                                         self.sequence_length])  # get from rollout policy and discriminator
        if has_input:
            # need to get the first index of each sentence to enforce rhyme
            self.inputs = tf.placeholder(tf.int32, shape=[self.batch_size,
                                                          self.sequence_length])
            self.processed_inputs = tf.transpose(tf.nn.embedding_lookup(self.g_embeddings,
                                                                        self.inputs), perm=[1, 0, 2])
        with tf.device("/cpu:0"):
            self.processed_x = tf.transpose(tf.nn.embedding_lookup(self.g_embeddings, self.x),
                                            perm=[1, 0, 2])  # seq_length x batch_size x emb_dim for LSTM

        # Initial states
        if has_input:
            with tf.variable_scope("encoder"):
                self.encoder_W = tf.Variable(tf.random_normal([self.emb_dim * self.sequence_length, self.hidden_dim]),
                                             name="W")
                self.encoder_b = tf.Variable(tf.zeros(self.hidden_dim), name='b')
            self.g_params.append(self.encoder_b)
            self.g_params.append(self.encoder_W)
            flatten = tf.reshape(self.processed_inputs, [self.batch_size, -1])
            densed = tf.nn.xw_plus_b(flatten, self.encoder_W, self.encoder_b)  # batch
            self.h0 = tf.stack([densed, densed])

        else:
            self.h0 = tf.zeros([self.batch_size, self.hidden_dim])  #
            self.h0 = tf.stack([self.h0, self.h0])  # hidden_state, cell_state
        gen_o = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)
        gen_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)

        # rhyme when infer
        def _g_recurrence_rhyme(i, x_t, h_tm1, gen_o, gen_x):
            h_t = self.g_recurrent_unit(x_t, h_tm1)
            o_t = self.g_output_unit(h_t)

            # first token, need to be rhymed according to probability
            def first():
                first_token = self.inputs[:, 0]  # (batch_size, 1)
                select_sampler = Bernoulli(
                    probs=1.0, dtype=tf.bool)
                select_sample = select_sampler.sample(
                    sample_shape=self.batch_size)
                token_rhyme = tf.cast(tf.gather(self.table, first_token), tf.float32)
                return tf.where(select_sample, tf.log(tf.multiply(token_rhyme, tf.nn.softmax(o_t))),
                                tf.log(tf.nn.softmax(o_t)))

            def other():
                return tf.log(tf.nn.softmax(o_t))

            if self.mode == 'infer':
                log_prob = tf.cond(tf.equal(i, tf.constant(0)), first, other)
            elif self.mode == 'train':
                log_prob = tf.log(tf.nn.softmax(o_t))

            # 根据 log_prob 采样下一个 word token
            next_token = tf.cast(tf.reshape(tf.multinomial(log_prob, 1), [self.batch_size]), tf.int32)

            # map next token to word embedding
            x_tp1 = tf.nn.embedding_lookup(self.g_embeddings, next_token)  # batch x emb_dim
            # save prob of the select token
            gen_o = gen_o.write(i, tf.reduce_sum(tf.multiply(tf.one_hot(next_token, self.num_emb, 1.0, 0.0),
                                                             tf.nn.softmax(o_t)), 1))  # [batch_size] , prob
            gen_x = gen_x.write(i, next_token)  # indices, batch_size , save token generated
            return i + 1, x_tp1, h_t, gen_o, gen_x

        _, _, _, self.gen_o, self.gen_x = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3, _4: i < self.sequence_length,
            body=_g_recurrence_rhyme,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(self.g_embeddings, self.start_token), self.h0, gen_o, gen_x))

        self.gen_x = self.gen_x.stack()  # seq_length x batch_size
        self.gen_x = tf.transpose(self.gen_x, perm=[1, 0])  # batch_size x seq_length

        # supervised loss
        g_predictions = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.sequence_length,
                                                     dynamic_size=False, infer_shape=True)
        ta_emb_x = tensor_array_ops.TensorArray(
            dtype=tf.float32, size=self.sequence_length)
        ta_emb_x = ta_emb_x.unstack(self.processed_x)  # embedded x : seq * batch_size *  emb_size

        # using the same lstm cell to generate tokens
        def _pretrain_recurrence(i, x_t, h_tm1, g_predictions):
            h_t = self.g_recurrent_unit(x_t, h_tm1)
            o_t = self.g_output_unit(h_t)

            g_predictions = g_predictions.write(i, tf.nn.softmax(o_t))  # batch x vocab_size
            # using the real token to generate next token
            x_tp1 = ta_emb_x.read(i)
            return i + 1, x_tp1, h_t, g_predictions

        _, _, _, self.g_predictions = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3: i < self.sequence_length,
            body=_pretrain_recurrence,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(self.g_embeddings, self.start_token),
                       self.h0,
                       g_predictions)
        )
        self.g_predictions = tf.transpose(self.g_predictions.stack(),
                                          perm=[1, 0, 2])  # batch_size x seq_length x vocab_size

        self.pretrain_loss = -tf.reduce_sum(
            tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])), self.num_emb, 1.0, 0.0) *
            tf.log(tf.clip_by_value(tf.reshape(self.g_predictions, [-1, self.num_emb]), 1e-20, 1.0))
        ) / (self.sequence_length * self.batch_size)

        # training update
        pretrain_opt = self.g_optimizer(self.learning_rate)

        self.pretrain_grad, _ = tf.clip_by_global_norm(tf.gradients(self.pretrain_loss, self.g_params), self.grad_clip)
        self.pretrain_updates = pretrain_opt.apply_gradients(zip(self.pretrain_grad, self.g_params))

        # penalty objective function
        self.g_loss = tf.reduce_sum(
            tf.reduce_sum(
                tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])), self.num_emb, 1.0, 0.0) *
                tf.clip_by_value(
                    tf.reshape(self.g_predictions, [-1, self.num_emb]), 1e-20, 1.0)
                , 1) * tf.reshape(self.rewards, [-1])  # * tf.reshape(self.target_weights, [-1])
        )

        g_opt = self.adv_optimizer(self.learning_rate)
        self.g_grad, _ = tf.clip_by_global_norm(tf.gradients(self.g_loss, self.g_params), self.grad_clip)
        self.g_update = g_opt.apply_gradients(zip(self.g_grad, self.g_params))

    def lstm_unit(self, params):
        # Weight and bias variables
        with tf.variable_scope("lstm"):
            self.Wi = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
            self.Ui = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
            self.bi = tf.Variable(self.init_matrix([self.hidden_dim]))

            self.Wf = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
            self.Uf = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
            self.bf = tf.Variable(self.init_matrix([self.hidden_dim]))

            self.Wog = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
            self.Uog = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
            self.bog = tf.Variable(self.init_matrix([self.hidden_dim]))

            self.Wc = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
            self.Uc = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
            self.bc = tf.Variable(self.init_matrix([self.hidden_dim]))

        params.extend([
            self.Wi, self.Ui, self.bi,
            self.Wog, self.Uog, self.bog,
            self.Wf, self.Uf, self.bf,
            self.Wc, self.Uc, self.bc
        ])

        def unit(x, hidden_memory):
            prev_h, prev_c = tf.unstack(hidden_memory)
            # Input Gate
            i = tf.sigmoid(
                tf.matmul(x, self.Wi) +
                tf.matmul(prev_h, self.Ui) + self.bi
            )

            # Forget Gate
            f = tf.sigmoid(
                tf.matmul(x, self.Wf) +
                tf.matmul(prev_h, self.Uf) + self.bf
            )

            # Output Gate
            o = tf.sigmoid(
                tf.matmul(x, self.Wog) +
                tf.matmul(prev_h, self.Uog) + self.bog
            )

            # candidate c
            c_ = tf.tanh(
                tf.matmul(x, self.Wc) +
                tf.matmul(prev_h, self.Uc) + self.bc
            )

            # update c
            c = f * prev_c + i * c_
            current_h = o * tf.nn.tanh(c)
            return tf.stack([current_h, c])

        return unit

    def init_matrix(self, shape):
        return tf.random_normal(shape, stddev=0.1)

    def init_vector(self, shape):  # may be used for the bias
        return tf.zeros(shape)

    def output_unit(self, params):
        with tf.variable_scope("output_unit"):
            self.Wo = tf.Variable(self.init_matrix([self.hidden_dim, self.num_emb]), name="W")
            self.bo = tf.Variable(self.init_matrix([self.num_emb]), name="b")
        params.extend([self.Wo, self.bo])

        def unit(hidden_memory_tuple):
            hidden_state, c_prev = tf.unstack(hidden_memory_tuple)
            # hidden_state : batch x hidden_dim
            logits = tf.nn.xw_plus_b(hidden_state, self.Wo, self.bo)
            # output = tf.nn.softmax(logits)
            return logits

        return unit

    def generate_(self, sess, inputs):
        return sess.run(self.gen_x, feed_dict={self.inputs: inputs})

    def pre_train(self, sess, x):
        outputs = sess.run([self.pretrain_updates, self.pretrain_loss],
                           feed_dict={self.x: x})
        return outputs

    def g_optimizer(self, *args, **kwargs):
        return tf.train.AdamOptimizer(*args, **kwargs)

    def adv_optimizer(self, *args, **kwargs):
        return tf.train.RMSPropOptimizer(*args, **kwargs)

    def pretrain_step(self, sess, input, target):
        '''x is ground truth'''
        outputs = sess.run([self.pretrain_updates, self.pretrain_loss], feed_dict={self.inputs: input, self.x: target})
        return outputs

    @staticmethod
    def restore(sess, saver, path):
        saver.restore(sess, save_path=path)
