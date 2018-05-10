"""
Word Language Model
===================

This example shows how to build a word-level language model on WikiText-2 with Gluon NLP Toolkit.
By using the existing data pipeline tools and building blocks, the process is greatly simplified.

We implement the AWD LSTM language model proposed in the following work.

@article{merityRegOpt,
  title={{Regularizing and Optimizing LSTM Language Models}},
  author={Merity, Stephen and Keskar, Nitish Shirish and Socher, Richard},
  journal={ICLR},
  year={2018}
}

Note that we are using standard SGD as the optimizer for code simpilification.
Once NT-ASGD in the work is implemented and used as the optimizer.
Our implementation should yield identical results.
"""

# coding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import argparse
import time
import math
import os
import sys
import mxnet as mx
from mxnet import gluon, autograd, init, nd
from mxnet.gluon import nn, Block, rnn
import gluonnlp as nlp

from LSTMPCellLSTMPCellWithClip import LSTMPCellWithClip

curr_path = os.path.dirname(os.path.abspath(os.path.expanduser(__file__)))
sys.path.append(os.path.join(curr_path, '..', '..'))


parser = argparse.ArgumentParser(description=
                                 'MXNet Autograd RNN/LSTM Language Model on Wikitext-2.')
parser.add_argument('--model', type=str, default='lstm',
                    help='type of recurrent net (rnn_tanh, rnn_relu, lstm, gru, lstmp)')
parser.add_argument('--emsize', type=int, default=400,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=1150,
                    help='number of hidden units per layer')
parser.add_argument('--cellclip', type=float, help='clip cell state between [-cellclip, projclip] in LSTMPCellWithClip')
parser.add_argument('--projsize', type=int, help='projection of nhid to projsize in LSTMPCellWithClip')
parser.add_argument('--projclip', type=float, help='clip projection between [-projclip, projclip] in LSTMPCellWithClip')
parser.add_argument('--nlayers', type=int, default=3,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=30,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=180,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=80, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=70,
                    help='sequence length')
parser.add_argument('--dropout', type=float, default=0.4,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--dropout_h', type=float, default=0.2,
                    help='dropout applied to hidden layer (0 = no dropout)')
parser.add_argument('--dropout_i', type=float, default=0.65,
                    help='dropout applied to input layer (0 = no dropout)')
parser.add_argument('--dropout_e', type=float, default=0.1,
                    help='dropout applied to embedding layer (0 = no dropout)')
parser.add_argument('--weight_dropout', type=float, default=0.5,
                    help='weight dropout applied to h2h weight matrix (0 = no weight dropout)')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str, default='model.params',
                    help='path to save the final model')
parser.add_argument('--eval_only', action='store_true',
                    help='Whether to only evaluate the trained model')
parser.add_argument('--gpus', type=str,
                    help='list of gpus to run, e.g. 0 or 0,2,5. empty means using cpu.'
                         '(using single gpu is suggested)')
parser.add_argument('--lr_update_interval', type=int, default=30,
                    help='lr udpate interval')
parser.add_argument('--lr_update_factor', type=float, default=0.1,
                    help='lr udpate factor')
parser.add_argument('--optimizer', type=str, default='sgd',
                    help='optimizer to use (sgd, adam)')
parser.add_argument('--wd', type=float, default=1.2e-6,
                    help='weight decay applied to all weights')
parser.add_argument('--skip_connection', action='store_true', help='add skip connections (add cell input to output)')
parser.add_argument('--char_embedding', action='store_true', help='Whether to use character embeddings or word embeddings')
parser.add_argument('--alpha', type=float, default=2,
                    help='alpha L2 regularization on RNN activation '
                         '(alpha = 0 means no regularization)')
parser.add_argument('--beta', type=float, default=1,
                    help='beta slowness regularization applied on RNN activiation '
                         '(beta = 0 means no regularization)')
parser.add_argument('--test_mode', action='store_true',
                    help='Whether to run through the script with few examples')
parser.add_argument('--load', action='store_true')
args = parser.parse_args()

def _get_rnn_cell(mode, num_layers, input_size, hidden_size, dropout, skip_connection, proj_size=None, cell_clip=None, proj_clip=None):
    """create rnn cell given specs"""
    rnn_cell = rnn.SequentialRNNCell()
    with rnn_cell.name_scope():
        for i in range(num_layers):
            if mode == 'rnn_relu':
                cell = rnn.RNNCell(hidden_size, 'relu', input_size=input_size)
            elif mode == 'rnn_tanh':
                cell = rnn.RNNCell(hidden_size, 'tanh', input_size=input_size)
            elif mode == 'lstm':
                cell = rnn.LSTMCell(hidden_size, input_size=input_size)
            elif mode == 'gru':
                cell = rnn.GRUCell(hidden_size, input_size=input_size)
            elif mode == 'lstmp':
                cell = LSTMPCellWithClip(hidden_size, proj_size, cell_clip=cell_clip, projection_clip=proj_clip, input_size=input_size)

            if skip_connection:
                cell = rnn.ResidualCell(cell)

            rnn_cell.add(cell)

            if dropout != 0:
                rnn_cell.add(rnn.DropoutCell(dropout))
    return rnn_cell

class ElmoLSTM(gluon.Block):
    def __init__(self, mode, num_layers, input_size, hidden_size, dropout, skip_connection, char_embedding, proj_size=None, cell_clip=None, proj_clip=None, weight_file=None, bidirectional=True):
        super(ElmoLSTM, self).__init__()

        self.num_layers = num_layers
        self.char_embedding = char_embedding
        self.weight_file = weight_file

        lstm_input_size = input_size

        with self.name_scope():
            for layer_index in range(num_layers):
                forward_layer = _get_rnn_cell(mode=mode, num_layers=1, input_size=lstm_input_size, hidden_size=hidden_size,
                                              dropout=0 if layer_index == num_layers - 1 else dropout, skip_connection=False if layer_index == 0 else skip_connection,
                                              proj_size=proj_size, cell_clip=cell_clip, proj_clip=proj_clip)
                backward_layer = _get_rnn_cell(mode=mode, num_layers=1, input_size=lstm_input_size, hidden_size=hidden_size,
                                              dropout=0 if layer_index == num_layers - 1 else dropout, skip_connection=False if layer_index == 0 else skip_connection,
                                              proj_size=proj_size, cell_clip=cell_clip, proj_clip=proj_clip)

                setattr(self, 'forward_layer_{}'.format(layer_index), forward_layer)
                setattr(self, 'backward_layer_{}'.format(layer_index), backward_layer)

                lstm_input_size = proj_size if mode == 'lstmp' else hidden_size

    def begin_state(self, *args, **kwargs):
        return [getattr(self, 'forward_layer_{}'.format(layer_index)).begin_state(*args, **kwargs) for layer_index in range(self.num_layers)],\
               [getattr(self, 'backward_layer_{}'.format(layer_index)).begin_state(*args, **kwargs) for layer_index in range(self.num_layers)]

    def forward(self, inputs, states):
        seq_len = inputs.shape[0] if self.char_embedding else inputs[0].shape[0]

        if not states:
            states_forward, states_backward = self.begin_state(batch_size=inputs.shape[1] if self.char_embedding else inputs[0].shape[1])
        else:
            states_forward, states_backward = states

        outputs_forward = []
        outputs_backward = []

        for j in range(self.num_layers):
            outputs_forward.append([])
            for i in range(seq_len):
                if j == 0:
                    output, states_forward[j] = getattr(self, 'forward_layer_{}'.format(j))(inputs[i] if self.char_embedding else inputs[0][i], states_forward[j])
                else:
                    output, states_forward[j] = getattr(self, 'forward_layer_{}'.format(j))(outputs_forward[j-1][i], states_forward[j])
                    # output = output + outputs_forward[j-1][i]
                outputs_forward[j].append(output)

            outputs_backward.append([None] * seq_len)
            for i in reversed(range(seq_len)):
                if j == 0:
                    output, states_backward[j] = getattr(self, 'backward_layer_{}'.format(j))(inputs[i] if self.char_embedding else inputs[1][i], states_backward[j])
                else:
                    output, states_backward[j] = getattr(self, 'backward_layer_{}'.format(j))(outputs_backward[j-1][i], states_backward[j])
                    # output = output + outputs_backward[j-1][i]
                outputs_backward[j][i] = output

        for i in range(self.num_layers):
            outputs_forward[i] = mx.nd.stack(*outputs_forward[i])
            outputs_backward[i] = mx.nd.stack(*outputs_backward[i])

        return (outputs_forward, outputs_backward), (states_forward, states_backward)

class ElmoBiLM(Block):
    """Standard RNN language model.

    Parameters
    ----------
    mode : str
        The type of RNN to use. Options are 'lstm', 'gru', 'rnn_tanh', 'rnn_relu'.
    vocab_size : int
        Size of the input vocabulary.
    embed_size : int
        Dimension of embedding vectors.
    hidden_size : int
        Number of hidden units for RNN.
    num_layers : int
        Number of RNN layers.
    dropout : float
        Dropout rate to use for encoder output.
    tie_weights : bool, default False
        Whether to tie the weight matrices of output dense layer and input embedding layer.
    """
    def __init__(self, mode, vocab_size, embed_size, hidden_size, num_layers, dropout=0.5, tie_weights=False, char_embedding=False,
                 skip_connection=False, proj_size=None, proj_clip=None, cell_clip=None, **kwargs):
        if tie_weights:
            assert embed_size == hidden_size, 'Embedding dimension must be equal to ' \
                                              'hidden dimension in order to tie weights. ' \
                                              'Got: emb: {}, hid: {}.'.format(embed_size,
                                                                              hidden_size)
        super(ElmoBiLM, self).__init__(**kwargs)
        self._mode = mode
        self._embed_size = embed_size
        self._hidden_size = hidden_size
        self._skip_connection = skip_connection
        self._proj_size = proj_size
        self._proj_clip = proj_clip
        self._cell_clip = cell_clip
        self._num_layers = num_layers
        self._dropout = dropout
        self._tie_weights = tie_weights
        self._vocab_size = vocab_size
        self._char_embedding = char_embedding

        with self.name_scope():
            self.embedding = self._get_embedding()
            self.encoder = self._get_encoder()
            self.decoder = self._get_decoder()

    def _get_embedding(self):
        embedding = nn.HybridSequential()
        with embedding.name_scope():
            embedding.add(nn.Embedding(self._vocab_size, self._embed_size,
                                       weight_initializer=init.Uniform(0.1)))
            if self._dropout:
                embedding.add(nn.Dropout(self._dropout))
        return embedding

    def _get_encoder(self):
        return ElmoLSTM(mode=self._mode, num_layers=self._num_layers, input_size=self._embed_size,
                              hidden_size=self._hidden_size, proj_size=self._proj_size, dropout=self._dropout,
                              skip_connection=self._skip_connection, char_embedding=self._char_embedding,
                              cell_clip=self._cell_clip, proj_clip=self._proj_clip)

    def _get_decoder(self):
        output = nn.HybridSequential()
        with output.name_scope():
            if self._tie_weights:
                output.add(nn.Dense(self._vocab_size, flatten=False,
                                    params=self.embedding[0].params))
            else:
                output.add(nn.Dense(self._vocab_size, flatten=False))
        return output

    def begin_state(self, *args, **kwargs):
        return self.encoder.begin_state(*args, **kwargs)

    def forward(self, inputs, begin_state=None): # pylint: disable=arguments-differ
        """Defines the forward computation. Arguments can be either
        :py:class:`NDArray` or :py:class:`Symbol`."""
        if self._char_embedding:
            encoded = self.embedding(inputs)
        else:
            encoded = self.embedding(inputs[0]), self.embedding(inputs[1])

        if not begin_state:
            begin_state = self.begin_state(batch_size=inputs.shape[1] if self._char_embedding else inputs[0].shape[1])

        encoded, state = self.encoder(encoded, begin_state)

        if self._dropout:
            encoded_forward = nd.Dropout(encoded[0][-1], p=self._dropout)
            encoded_backward = nd.Dropout(encoded[1][-1], p=self._dropout)
        else:
            encoded_forward = encoded[0][-1]
            encoded_backward = encoded[1][-1]

        forward_out = self.decoder(encoded_forward)
        backward_out = self.decoder(encoded_backward)

        return (forward_out, backward_out), state

###############################################################################
# Load data
###############################################################################

context = [mx.cpu()] if args.gpus is None or args.gpus == '' else \
          [mx.gpu(int(x)) for x in args.gpus.split(',')]

assert args.batch_size % len(context) == 0, \
    'Total batch size must be multiple of the number of devices'

assert args.weight_dropout > 0 or (args.weight_dropout == 0 and args.alpha == 0), \
    'The alpha L2 regularization cannot be used with standard RNN, please set alpha to 0'

train_dataset, val_dataset, test_dataset = \
    [nlp.data.WikiText2(segment=segment,
                        skip_empty=False, bos=None, eos='<eos>')
     for segment in ['train', 'val', 'test']]

vocab = nlp.Vocab(counter=nlp.data.Counter(train_dataset[0]), padding_token=None, bos_token=None)

train_data = train_dataset.batchify(vocab, args.batch_size)
val_batch_size = args.batch_size
val_data = val_dataset.batchify(vocab, val_batch_size)
test_batch_size = args.batch_size
test_data = test_dataset.batchify(vocab, test_batch_size)

if args.test_mode:
    args.emsize = 200
    args.nhid = 200
    args.nlayers = 1
    args.epochs = 3
    train_data = train_data[0:100]
    val_data = val_data[0:100]
    test_data = test_data[0:100]

print(args)

###############################################################################
# Build the model
###############################################################################


ntokens = len(vocab)

model = ElmoBiLM(mode=args.model, vocab_size=len(vocab), embed_size=args.emsize, hidden_size=args.nhid, num_layers=args.nlayers,
                 tie_weights=args.tied, dropout=args.dropout, skip_connection=args.skip_connection, proj_size=args.projsize,
                 proj_clip=args.projclip, cell_clip=args.cellclip, char_embedding=args.char_embedding)

print(model)
model.initialize(mx.init.Xavier(), ctx=context)
model.hybridize()

if args.optimizer == 'sgd':
    trainer_params = {'learning_rate': args.lr,
                      'momentum': 0,
                      'wd': args.wd}
elif args.optimizer == 'adam':
    trainer_params = {'learning_rate': args.lr,
                      'wd': args.wd,
                      'beta1': 0,
                      'beta2': 0.999,
                      'epsilon': 1e-9}
elif args.optimizer == 'adagrad':
    trainer_params = {'learning_rate': args.lr,
                      'wd': args.wd}

trainer = gluon.Trainer(model.collect_params(), args.optimizer, trainer_params)
loss = gluon.loss.SoftmaxCrossEntropyLoss()

###############################################################################
# Training code
###############################################################################

def get_batch(data_source, i, seq_len=None):
    seq_len = min(seq_len if seq_len else args.bptt, len(data_source) - 1 - i)
    data = data_source[i:i+seq_len]
    target = data_source[i+1:i+1+seq_len]
    return data, target

def detach(hidden):
    if isinstance(hidden, (tuple, list)):
        hidden = [detach(h) for h in hidden]
    else:
        hidden = hidden.detach()
    return hidden

def get_ppl(cur_loss):
    try:
        ppl = math.exp(cur_loss)
    except:
        ppl = float('inf')
    return ppl

def evaluate(data_source, batch_size, ctx=None):
    """Evaluate the model on the dataset.

    Parameters
    ----------
    data_source : NDArray
        The dataset is evaluated on.
    batch_size : int
        The size of the mini-batch.
    ctx : mx.cpu() or mx.gpu()
        The context of the computation.

    Returns
    -------
    loss: float
        The loss on the dataset
    """
    total_L = 0.0
    ntotal = 0
    hidden = model.begin_state(batch_size=batch_size, func=mx.nd.zeros, ctx=context[0])
    for i in range(0, len(data_source) - 1, args.bptt):
        data, target = get_batch(data_source, i)
        data = data.as_in_context(ctx)
        target = target.as_in_context(ctx)
        output, hidden = model((data, target), hidden)
        hidden = detach(hidden)
        L = loss(output[0].reshape(-3, -1),
                 target.reshape(-1,))
        total_L += mx.nd.sum(L).asscalar()

        L = loss(output[1].reshape(-3, -1),
                 data.reshape(-1,))
        total_L += mx.nd.sum(L).asscalar()

        ntotal += 2 * L.size
    return total_L / ntotal


def forward(inputs, begin_state=None):
    """Implement forward computation using awd language model.

    Parameters
    ----------
    inputs : NDArray
        The training dataset.
    begin_state : list
        The initial hidden states.

    Returns
    -------
    out: NDArray
        The output of the model.
    out_states: list
        The list of output states of the model's encoder.
    encoded_raw: list
        The list of outputs of the model's encoder.
    encoded_dropped: list
        The list of outputs with dropout of the model's encoder.
    """
    if model._char_embedding:
        encoded = model.embedding(inputs)
    else:
        encoded = model.embedding(inputs[0]), model.embedding(inputs[1])

    if not begin_state:
        begin_state = model.begin_state(batch_size=inputs.shape[1] if model._char_embedding else inputs[0].shape[1])
    out_states = []
    encoded_raw = []
    encoded_dropped = []

    encoded, state = model.encoder(encoded, begin_state)
    encoded_raw.append(encoded)

    if model._dropout:
        encoded_forward = nd.Dropout(encoded[0][-1], p=model._dropout)
        encoded_backward = nd.Dropout(encoded[1][-1], p=model._dropout)
    else:
        encoded_forward = encoded[0][-1]
        encoded_backward = encoded[1][-1]

    forward_out = model.decoder(encoded_forward)
    backward_out = model.decoder(encoded_backward)

    return (forward_out, backward_out), state, encoded_raw, encoded_dropped

def criterion(output, target, encoder_hs, dropped_encoder_hs):
    """Compute regularized (optional) loss of the language model in training mode.

        Parameters
        ----------
        output: NDArray
            The output of the model.
        target: list
            The list of output states of the model's encoder.
        encoder_hs: list
            The list of outputs of the model's encoder.
        dropped_encoder_hs: list
            The list of outputs with dropout of the model's encoder.

        Returns
        -------
        l: NDArray
            The loss per word/token.
            If both args.alpha and args.beta are zeros, the loss is the standard cross entropy.
            If args.alpha is not zero, the standard loss is regularized with activation.
            If args.beta is not zero, the standard loss is regularized with temporal activation.
    """
    l = loss(output.reshape(-3, -1), target.reshape(-1,))
    if args.alpha:
        dropped_means = [args.alpha*dropped_encoder_h.__pow__(2).mean()
                         for dropped_encoder_h in dropped_encoder_hs[-1:]]
        l = l + mx.nd.add_n(*dropped_means)
    if args.beta:
        means = [args.beta*(encoder_h[1:] - encoder_h[:-1]).__pow__(2).mean()
                 for encoder_h in encoder_hs[-1:]]
        l = l + mx.nd.add_n(*means)
    return l

def train():
    """Training loop for awd language model.

    """
    best_val = float('Inf')
    start_train_time = time.time()
    parameters = model.collect_params().values()
    for epoch in range(args.epochs):
        total_L = 0.0
        start_epoch_time = time.time()
        start_log_interval_time = time.time()
        hiddens = [model.begin_state(batch_size=args.batch_size//len(context),
                                     func=mx.nd.zeros, ctx=ctx) for ctx in context]
        batch_i, i = 0, 0
        while i < len(train_data) - 1 - 1:
            seq_len = args.bptt

            data, target = get_batch(train_data, i, seq_len=seq_len)
            data_list = gluon.utils.split_and_load(data, context, batch_axis=1, even_split=True)
            target_list = gluon.utils.split_and_load(target, context, batch_axis=1, even_split=True)
            hiddens = detach(hiddens)
            Ls = []
            L = 0
            with autograd.record():
                for j, (X, y, h) in enumerate(zip(data_list, target_list, hiddens)):
                    output, h, encoder_hs, dropped_encoder_hs = forward((X, y), h)
                    l = criterion(output[0], y, encoder_hs, dropped_encoder_hs)
                    L = L + l.as_in_context(context[0]) / X.size
                    Ls.append(l/X.size)

                    l = criterion(output[1], X, encoder_hs, dropped_encoder_hs)
                    L = L + l.as_in_context(context[0]) / X.size
                    Ls.append(l/X.size)

                    hiddens[j] = h
            L.backward()
            grads = [p.grad(d.context) for p in parameters for d in data_list]
            gluon.utils.clip_global_norm(grads, args.clip)

            trainer.step(1)

            total_L += sum([mx.nd.sum(L).asscalar() for L in Ls]) / 2
            if batch_i % args.log_interval == 0 and batch_i > 0:
                cur_L = total_L / args.log_interval
                print('[Epoch %d Batch %d/%d] loss %.2f, ppl %.2f, '
                      'throughput %.2f samples/s, lr %.2f'
                      %(epoch, batch_i, len(train_data)//args.bptt, cur_L, math.exp(cur_L),
                        args.batch_size*args.log_interval/(time.time()-start_log_interval_time),
                        trainer.learning_rate))
                total_L = 0.0
                start_log_interval_time = time.time()
            i += seq_len
            batch_i += 1

        mx.nd.waitall()

        print('[Epoch %d] throughput %.2f samples/s'%(
            epoch, (args.batch_size * len(train_data)) / (time.time() - start_epoch_time)))
        val_L = evaluate(val_data, val_batch_size, context[0])
        print('[Epoch %d] time cost %.2fs, valid loss %.2f, valid ppl %.2f'%(
            epoch, time.time()-start_epoch_time, val_L, math.exp(val_L)))

        if val_L < best_val:
            update_lr_epoch = 0
            best_val = val_L
            test_L = evaluate(test_data, test_batch_size, context[0])
            model.save_params(args.save)
            print('test loss %.2f, test ppl %.2f'%(test_L, math.exp(test_L)))
        else:
            update_lr_epoch += 1
            if update_lr_epoch % args.lr_update_interval == 0 and update_lr_epoch != 0:
                lr_scale = trainer.learning_rate * args.lr_update_factor
                print('Learning rate after interval update %f'%(lr_scale))
                trainer.set_learning_rate(lr_scale)
                update_lr_epoch = 0

    print('Total training throughput %.2f samples/s'
          %((args.batch_size * len(train_data) * args.epochs) / (time.time() - start_train_time)))


if __name__ == '__main__':
    start_pipeline_time = time.time()
    if args.load:
        model.load_params(args.save, context)
    if not args.eval_only:
        train()
    model.load_params(args.save, context)
    final_val_L = evaluate(val_data, val_batch_size, context[0])
    final_test_L = evaluate(test_data, test_batch_size, context[0])
    print('Best validation loss %.2f, val ppl %.2f'%(final_val_L, math.exp(final_val_L)))
    print('Best test loss %.2f, test ppl %.2f'%(final_test_L, math.exp(final_test_L)))
    print('Total time cost %.2fs'%(time.time()-start_pipeline_time))