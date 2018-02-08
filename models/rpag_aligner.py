'''
Created on Jan 10, 2018

@author: gcampagn
'''

import tensorflow as tf
from tensorflow.contrib.rnn import LSTMStateTuple

from collections import namedtuple

from .base_aligner import BaseAligner
from .seq2seq_aligner import Seq2SeqAligner
from . import common

# in paper notation:
# context_vector is psi
# committment_vector is c
# alignment_vector is alpha
RPAGDecoderState = namedtuple('RPAGDecoderState', ('cell_state', 'context_vector', 'commitment_vector', 'alignment_vector', 'alignment_history'))
RPAGDecoderOutput = namedtuple('RPAGDecoderOutput', ('rnn_output', 'sample_id'))

class RPAGDecoder(tf.contrib.seq2seq.Decoder):
    def __init__(self, cell, embedding, start_tokens, end_token, initial_state, enc_hidden_states, enc_length, plan_length, output_layer=None):
        self._cell = cell
        self._output_layer = output_layer
        self._embedding_fn = lambda ids: tf.nn.embedding_lookup(embedding, ids)

        self._output_size = output_layer.units if output_layer is not None else self._output.output_size
        self._batch_size = tf.size(start_tokens)
        self._start_tokens = start_tokens
        self._end_token = end_token
        self._initial_cell_state = initial_state
        self._enc_hidden_states = enc_hidden_states
        
        enc_shape = tf.shape(enc_hidden_states)
        self._enc_max_time = enc_hidden_states.shape[1] or enc_shape[1]
        self._enc_length_mask = tf.sequence_mask(enc_length, self._enc_max_time, name='enc_length_mask')
        self._plan_length = plan_length
        
        self._context_vector_size = enc_hidden_states.shape[2] or enc_shape[2]
        
        self._align_layer = tf.layers.Dense(self._context_vector_size, use_bias=False, name='align_layer')
        self._commit_update_layer = tf.layers.Dense(self._plan_length, name='commit_update_layer')
        
    @property
    def batch_size(self):
        return self._batch_size
    
    @property
    def output_size(self):
        # Return the cell output and the id
        return RPAGDecoderOutput(rnn_output=tf.TensorShape((self._output_size,)),
                                 sample_id=tf.TensorShape(()))
        
    @property
    def output_dtype(self):
        return RPAGDecoderOutput(rnn_output=tf.float32,
                                 sample_id=tf.int32)
    
    def initialize(self, name=None):
        with tf.name_scope(name, 'RPAGDecoderInitialize'):
            initial_commit = tf.concat((tf.ones((self._batch_size,1), dtype=tf.float32),
                                        tf.zeros((self._batch_size,self._plan_length-1), dtype=tf.float32)), axis=1)
            
            initial_state = RPAGDecoderState(cell_state=self._initial_cell_state,
                                             context_vector=tf.zeros((self._batch_size, self._context_vector_size,), dtype=tf.float32),
                                             commitment_vector=initial_commit,
                                             alignment_vector=tf.zeros((self._batch_size, self._enc_max_time,), dtype=tf.float32),
                                             alignment_history=tf.TensorArray(dtype=tf.float32, size=0, dynamic_size=True))
            
            first_inputs = self._embedding_fn(self._start_tokens)
            finished = tf.zeros((self._batch_size,), dtype=tf.bool)
            
            return finished, first_inputs, initial_state
    
    def step(self, time, inputs, state, name=None):
        with tf.name_scope(name, 'RPAGDecoderStep', (time, inputs, state)):
            max_index = tf.argmax(state.commitment_vector, axis=1)
            switch_value = tf.equal(max_index, tf.zeros((self._batch_size,), dtype=tf.int64), name='switch_value')
            print('switch_value', switch_value)
            
            with tf.name_scope('if_switch_true'):
                # remove c from the flat cell state, only consider h, in both directions;
                # this is more correct because h is the real recurrent state and c is just
                # a memory cell that the LSTM uses internally
                def ignore_c_state(structure):
                    if isinstance(structure, LSTMStateTuple):
                        return structure.h
                    elif isinstance(structure, tuple):
                        return tuple(ignore_c_state(x) for x in structure)
                    else:
                        return structure
                h_cell_state = ignore_c_state(state.cell_state)
                
                flat_cell_state = tf.concat(h_cell_state, axis=1, name='flat_cell_state')
                print('flat_cell_state', flat_cell_state)
                with tf.name_scope('commitment_vector'):
                    flat_commit_input = tf.concat((flat_cell_state, state.context_vector), axis=1)
                    print('flat_commit_input', flat_commit_input)
                    
                    if_switch_true_commit_vector = tf.nn.softmax(self._commit_update_layer(flat_commit_input))
                with tf.name_scope('alignment_vector'):
                    processed_memory = self._align_layer(self._enc_hidden_states)
                    processed_memory = tf.reshape(processed_memory, (self._batch_size, self._enc_max_time, self._context_vector_size))
                    print('processed_memory', processed_memory)
                    
                    # Luong-style attention scoring
                    unnorm_align_vector = tf.squeeze(tf.matmul(tf.expand_dims(flat_cell_state, axis=1), processed_memory, transpose_b=True), axis=1)
                    print('unnorm_align_vector', unnorm_align_vector)

                    unnorm_align_vector = tf.where(self._enc_length_mask, unnorm_align_vector, tf.fill((self._batch_size, self._enc_max_time), float('-inf')))
                    
                    if_switch_true_align_vector = tf.nn.softmax(unnorm_align_vector, dim=1)
            with tf.name_scope('if_switch_false'):
                with tf.name_scope('commitment_vector'):
                    if_switch_false_commit_vector = tf.concat((state.commitment_vector[:,1:], tf.zeros((self._batch_size, 1), dtype=tf.float32)), axis=1)
                if_switch_false_align_vector = state.alignment_vector
        
            commit_vector = tf.where(switch_value, if_switch_true_commit_vector, if_switch_false_commit_vector, name='committment_vector')
            align_vector = tf.where(switch_value, if_switch_true_align_vector, if_switch_false_align_vector, name='alignment_vector')
        
            with tf.name_scope('context_vector'):
                context_vector = tf.squeeze(tf.matmul(tf.expand_dims(align_vector, axis=1), self._enc_hidden_states), axis=1)
        
            rnn_output, rnn_state = self._cell(tf.concat((inputs, context_vector), axis=1), state.cell_state)
            
            cell_output = self._output_layer(rnn_output) if self._output_layer else rnn_output
            
            sample_ids = tf.cast(tf.argmax(cell_output, axis=1), tf.int32)
            finished = tf.equal(sample_ids, self._end_token)
            next_inputs = self._embedding_fn(sample_ids)
            
            output = RPAGDecoderOutput(rnn_output=cell_output, sample_id=sample_ids)
            next_state = RPAGDecoderState(cell_state=rnn_state,
                                          context_vector=context_vector,
                                          commitment_vector=commit_vector,
                                          alignment_vector=align_vector,
                                          alignment_history=state.alignment_history.write(time, align_vector))
            
            return (output, next_state, next_inputs, finished)


class RPAGAligner(Seq2SeqAligner):
    '''
    "repeat, plan, attend, generate" decoder from
    "Plan, Attend, Generate: Planning for Sequence-to-Sequence Models",
    
    Dutil, Gulcehre, et al., NIPS 2017
    '''

    def add_decoder_op(self, enc_final_state, enc_hidden_states, training):
        cell_dec = common.make_multi_rnn_cell(self.config.rnn_layers, self.config.rnn_cell_type,
                                              self.config.output_embed_size + self.config.decoder_hidden_size,
                                              self.config.decoder_hidden_size,
                                              self.dropout_placeholder)
        enc_hidden_states, enc_final_state = common.unify_encoder_decoder(cell_dec,
                                                                          enc_hidden_states,
                                                                          enc_final_state)
        
        output_layer = tf.layers.Dense(self.config.grammar.output_size, use_bias=False)
        
        go_vector = tf.ones((self.batch_size,), dtype=tf.int32) * self.config.grammar.start
        decoder = RPAGDecoder(cell_dec, self.output_embed_matrix, go_vector, self.config.grammar.end,
                              enc_final_state, enc_hidden_states, self.input_length_placeholder,
                              plan_length=10, output_layer=output_layer)
        final_outputs, final_state, _ = tf.contrib.seq2seq.dynamic_decode(decoder,
                                                                          impute_finished=True,
                                                                          maximum_iterations=self.config.max_length,
                                                                          swap_memory=True)
        
        # convert alignment history from time-major to batch major
        self.attention_scores = tf.transpose(final_state.alignment_history.stack(), [1, 0, 2])
        
        return final_outputs
