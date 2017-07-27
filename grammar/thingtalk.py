
import tensorflow as tf
import numpy as np

from orderedset import OrderedSet
from collections import OrderedDict

import sys

from .abstract import AbstractGrammar

ENTITIES = ['USERNAME', 'HASHTAG',
            'QUOTED_STRING', 'NUMBER',
            'PHONE_NUMBER', 'EMAIL_ADDRESS', 'URL',
            'DATE', 'TIME', 'DURATION',
            'LOCATION']

BEGIN_TOKENS = ['special', 'answer', 'command', 'rule', 'trigger', 'query', 'action']
SPECIAL_TOKENS = ['tt:root.special.yes', 'tt:root.special.no', 'tt:root.special.nevermind',
                  'tt:root.special.makerule', 'tt:root.special.failed']
#IF = 'if'
#THEN = 'then'
OPERATORS = ['is', 'contains', '>', '<', 'has']
VALUES = ['true', 'false', 'absolute', 'rel_home', 'rel_work', 'rel_current_location', '1', '0']
TYPES = {
    'Location': (['is'], ['LOCATION', 'rel_current_location', 'rel_work', 'rel_home']),
    'Boolean':  (['is'], ['true', 'false']),
    'Bool': (['is'], ['true', 'false']),
    'String': (['is', 'contains'], ['QUOTED_STRING']),
    'Date': (['is'], ['DATE']),
    'Time': (['is'], ['TIME']),
    'Number': (['is', '<', '>'], ['NUMBER', '1', '0']),
    'Entity(tt:contact)': (['is'], ['USERNAME', 'QUOTED_STRING']),
    'Entity(tt:username)': (['is'], ['USERNAME', 'QUOTED_STRING']),
    'Entity(tt:hashtag)': (['is'], ['HASHTAG', 'QUOTED_STRING']),
    'Entity(tt:phone_number)': (['is'], ['USERNAME', 'PHONE_NUMBER', 'QUOTED_STRING']),
    'Entity(tt:email_address)': (['is'], ['USERNAME', 'EMAIL_ADDRESS', 'QUOTED_STRING']),
    'Entity(tt:url)': (['is'], ['URL', 'QUOTED_STRING']),
    'Entity(tt:picture)': (['is'], [])
}
TYPE_RENAMES = {
    'Username': 'Entity(tt:username)',
    'Hashtag': 'Entity(tt:hashtag)',
    'PhoneNumber': 'Entity(tt:phone_number)',
    'EmailAddress': 'Entity(tt:email_address)',
    'URL': 'Entity(tt:url)',
    'Picture': 'Entity(tt:picture)'
}

UNITS = dict(C=["C", "F"],
             ms=["ms", "s", "min", "h", "day", "week", "month", "year"],
             m=["m", "km", "mm", "cm", "mi", "in", "ft"],
             mps=["mps", "kmph", "mph"],
             kg=["kg", "g", "lb", "oz"],
             kcal=["kcal", "kJ"],
             bpm=["bpm"],
             byte=["byte", "KB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB"])

COMMAND_TOKENS = ['list', 'help', 'generic', 'device', 'command', 'make', 'rule', 'configure', 'discover']

MAX_ARG_VALUES = 8

class ThingtalkGrammar(AbstractGrammar):
    def __init__(self, filename):
        super().__init__()
        
        triggers = OrderedDict()
        queries = OrderedDict()
        actions = OrderedDict()
        functions = dict(trigger=triggers, query=queries, action=actions)
        self.functions = functions
        self.entities = OrderedSet()
        devices = []
        trigger_or_query_params = set()

        tokens = OrderedSet()
        tokens.update(BEGIN_TOKENS)
        #tokens.add(IF)
        #tokens.add(THEN)
        tokens.update(OPERATORS)
        tokens.update(VALUES)
        tokens.update(COMMAND_TOKENS)
        tokens.update(SPECIAL_TOKENS)
        
        for unitlist in UNITS.values():
            tokens.update(unitlist)
        tokens.add('tt:param.$event')
        trigger_or_query_params.add('tt:param.$event')
        
        enum_types = dict()
        
        with open(filename, 'r') as fp:
            for line in fp.readlines():
                line = line.strip().split()
                function_type = line[0]
                function = line[1]
                if function_type == 'device':
                    devices.append(function)
                    tokens.add(function)
                    continue
                if function_type == 'entity':
                    self.entities.add(function)
                    continue

                parameters = line[2:]
                paramlist = []
                functions[function_type][function] = paramlist
                tokens.add(function)
                
                for i in range(len(parameters)//2):
                    param = parameters[2*i]
                    type = parameters[2*i+1]
                    
                    paramlist.append((param, type))
                    tokens.add('tt:param.' + param)
                    if function_type != 'action':
                        trigger_or_query_params.add('tt:param.' + param)
                    
                    if type.startswith('Array('):
                        elementtype = type[len('Array('):-1]
                    else:
                        elementtype = type
                    if elementtype.startswith('Enum('):
                        enums = elementtype[len('Enum('):-1].split(',')
                        for enum in enums:
                            tokens.add(enum)
                        if not elementtype in enum_types:
                            enum_types[elementtype] = enums
        
        for i in range(MAX_ARG_VALUES):
            for entity in ENTITIES:
                tokens.add(entity + "_" + str(i))
        for generic_entity in self.entities:
            for i in range(MAX_ARG_VALUES):
                tokens.add('GENERIC_ENTITY_' + generic_entity + "_" + str(i))
        
        self.tokens = ['<<PAD>>', '<<EOS>>', '<<GO>>'] + list(tokens)
        self.dictionary = dict()
        for i, token in enumerate(self.tokens):
            self.dictionary[token] = i
        
        # build a DFA that will parse the thingtalk-ish code

        states = []
        transitions = []
        state_names = []
        
        def to_ids(tokens, words):
            return list([words[x] for x in tokens])

        def add_allowed_tokens(state, tokens):
            state[to_ids(tokens, self.dictionary)] = 1
        
        def new_state(name):
            state = np.zeros((self.output_size,))
            states.append(state)
            state_names.append(name)
            return len(states)-1
        
        # start with one of the begin tokens
        self.start_state = new_state('start')
        
        # in the before end state we just wait for EOS
        self.before_end_state = new_state('before_end')
        
        # in the end state we are done
        self.end_state = new_state('end')
        transitions.append((self.before_end_state, self.end_state, '<<EOS>>'))
        transitions.append((self.end_state, self.end_state, '<<PAD>>'))
        
        # special
        special_id = new_state('special')
        transitions.append((self.start_state, special_id, 'special'))
        for t in SPECIAL_TOKENS:
            transitions.append((special_id, self.before_end_state, t))
            
        # command
        command_id = new_state('command')
        transitions.append((self.start_state, command_id, 'command'))
        # list command
        list_id = new_state('list')
        transitions.append((command_id, list_id, 'list'))
        for t in ['generic', 'device', 'command']:
            transitions.append((list_id, self.before_end_state, t))
        # help/configure/discover command
        help_id = new_state('help_configure_discover')
        for t in ['help', 'configure', 'discover']:
            transitions.append((command_id, help_id, t))
        transitions.append((help_id, self.before_end_state, 'generic'))
        for d in devices:
            transitions.append((help_id, self.before_end_state, d))
        # make rule
        make_id = new_state('make')
        transitions.append((command_id, make_id, 'make'))
        transitions.append((make_id, self.before_end_state, 'rule'))
        
        # answers
        answer_id = new_state('answer')
        transitions.append((self.start_state, answer_id, 'answer'))
        for v in VALUES:
            if v != '0' and v != '1':
                transitions.append((answer_id, self.before_end_state, v))
        for v in ENTITIES:
            if v != 'NUMBER':
                for i in range(MAX_ARG_VALUES):
                    transitions.append((answer_id, self.before_end_state, v + '_' + str(i)))
        before_unit = new_state('answer_before_unit')
        for i in range(MAX_ARG_VALUES):
            transitions.append((answer_id, before_unit, 'NUMBER_' + str(i)))
        transitions.append((answer_id, before_unit, '0'))
        transitions.append((answer_id, before_unit, '1'))
        transitions.append((before_unit, self.end_state, '<<EOS>>'))
        for base_unit in UNITS:
            for unit in UNITS[base_unit]:
                transitions.append((before_unit, self.before_end_state, unit))
        
        # primitives
        actions_id = new_state('action')
        transitions.append((self.start_state, actions_id, 'action'))
        queries_id = new_state('query')
        transitions.append((self.start_state, queries_id, 'query'))
        triggers_id = new_state('trigger')
        transitions.append((self.start_state, triggers_id, 'trigger'))
        
        def do_invocation(invocation_name, params, for_action=False, can_have_scope=False):
            state_id = new_state(invocation_name)
            
            # allow one USERNAME_ parameter to follow the invocation immediately
            for i in range(MAX_ARG_VALUES):
                transitions.append((state_id, state_id, 'USERNAME_' + str(i)))
            
            # go to each parameter
            for param_name, param_type in params:
                if param_type in ('Any'):
                    continue
                if param_type in ('Picture', 'Array(Picture)', 'Entity(tt:picture)',
                    'Entity(tt:picture)') and not can_have_scope:
                    continue
                elementtype = param_type
                is_array = False
                is_measure = False
                if param_type.startswith('Array('):
                    is_array = True
                    elementtype = param_type[len('Array('):-1]
                if elementtype in TYPE_RENAMES:
                    elementtype = TYPE_RENAMES[elementtype]
                if elementtype.startswith('Measure('):
                    is_measure = True
                    operators = ['is', '<', '>']
                    base_unit = elementtype[len('Measure('):-1]
                    values = UNITS[base_unit]
                elif elementtype.startswith('Enum('):
                    operators = ['is']
                    values = enum_types[elementtype]
                elif elementtype == 'Entity(tt:device)':
                    operators = ['is']
                    values = devices
                elif elementtype in TYPES:
                    operators, values = TYPES[elementtype]
                elif elementtype.startswith('Entity('):
                    operators = ['is']
                    values = ['GENERIC_ENTITY_' + elementtype[len('Entity('):-1], 'QUOTED_STRING']
                else:
                    operators, values = TYPES[elementtype]
                if is_array:
                    if for_action:
                        continue
                    else:
                        operators = ['has']
                elif for_action:
                    operators = ['is']
                
                before_op = new_state(invocation_name + '_tt:param.' + param_name)
                transitions.append((state_id, before_op, 'tt:param.' + param_name))
                before_value = new_state(invocation_name + '_tt:param.' + param_name + '_value')

                for op in operators:
                    transitions.append((before_op, before_value, op))
                if is_measure:
                    before_unit = new_state(invocation_name + '_tt:param.' + param_name + '_unit')
                    for i in range(MAX_ARG_VALUES):
                        transitions.append((before_value, before_unit, '0'))
                        transitions.append((before_value, before_unit, '1'))
                        transitions.append((before_value, before_unit, 'NUMBER_' + str(i)))
                    for unit in values:
                        transitions.append((before_unit, state_id, unit))
                else:
                    for v in values:
                        if v[0].isupper():
                            for i in range(MAX_ARG_VALUES):
                                transitions.append((before_value, state_id, v + '_' + str(i)))
                        else:
                            transitions.append((before_value, state_id, v))
                if is_measure and base_unit == 'ms':
                    for i in range(MAX_ARG_VALUES):
                        transitions.append((before_value, state_id, 'DURATION_' + str(i)))
                if can_have_scope:
                    for v in trigger_or_query_params:
                        transitions.append((before_value, state_id, v))
                    
            return state_id

        for action_name, params in actions.items():
            state_id = do_invocation(action_name, params, for_action=True)
            transitions.append((actions_id, state_id, action_name))
            transitions.append((state_id, self.end_state, '<<EOS>>'))
        for query_name, params in queries.items():
            state_id = do_invocation(query_name, params, for_action=False)
            transitions.append((queries_id, state_id, query_name))
            transitions.append((state_id, self.end_state, '<<EOS>>'))
        for trigger_name, params in triggers.items():
            state_id = do_invocation(trigger_name, params, for_action=False)
            transitions.append((triggers_id, state_id, trigger_name))
            transitions.append((state_id, self.end_state, '<<EOS>>'))
        
        # rules
        rule_id = new_state('rule')
        transitions.append((self.start_state, rule_id, 'rule'))
        #if_id = new_state('if')
        #transitions.append((rule_id, if_id, 'if'))
        #then_to_query_id = new_state('then_to_query_or_action')
        #then_to_action_id = new_state('then_to_action')
        trigger_ids = []
        query_ids = []
        for trigger_name, params in triggers.items():
            state_id = do_invocation(trigger_name, params, for_action=False)
            transitions.append((rule_id, state_id, trigger_name))
            #transitions.append((state_id, then_to_query_id, 'then'))
            trigger_ids.append(state_id)
        for query_name, params in queries.items():
            state_id = do_invocation(query_name, params, for_action=False)
            transitions.append((rule_id, state_id, query_name))
            query_ids.append(state_id)

            state_id = do_invocation(query_name, params, for_action=False, can_have_scope=True)
            #transitions.append((state_id, then_to_action_id, 'then'))
            for trigger_id in trigger_ids:
                transitions.append((trigger_id, state_id, query_name))
            query_ids.append(state_id)
            transitions.append((state_id, self.end_state, '<<EOS>>'))

        for action_name, params in actions.items():
            state_id = do_invocation(action_name, params, for_action=True, can_have_scope=True)
            for trigger_id in trigger_ids:
                transitions.append((trigger_id, state_id, action_name))
            for query_id in query_ids:
                transitions.append((query_id, state_id, action_name))
            transitions.append((state_id, self.end_state, '<<EOS>>'))

        # now build the actual DFA
        num_states = len(states)
        self.num_states = num_states
        print("num states", num_states)
        print("num tokens", self.output_size)
        self.transition_matrix = np.zeros((num_states, self.output_size), dtype=np.int32)
        self.transition_matrix.fill(-1)
        self.allowed_token_matrix = np.zeros((num_states, self.output_size), dtype=np.bool8)

        for from_state, to_state, token in transitions:
            token_id = self.dictionary[token]
            if self.transition_matrix[from_state, token_id] != -1 and \
                self.transition_matrix[from_state, token_id] != to_state:
                raise ValueError("Ambiguous transition around token " + token + " in state " + state_names[from_state])
            self.transition_matrix[from_state, token_id] = to_state
            self.allowed_token_matrix[from_state, token_id] = True

        if True:
            visited = set()
            def dfs(state):
                visited.add(state)
                any_out = False
                for next_state in self.transition_matrix[state]:
                    if next_state == -1:
                        continue
                    any_out = True
                    if next_state in visited:
                        continue
                    dfs(next_state)
                if not any_out:
                    raise ValueError('Reachable state %d (%s) has no outgoing states' % (state, state_names[state]))
            dfs(self.start_state)

        self.state_names = state_names


    def get_embeddings(self, use_types=False):
        if not use_types:
            return np.identity(self.output_size, np.float32)
        
        num_entity_tokens = (len(ENTITIES) + len(self.entities)) * MAX_ARG_VALUES
        num_other_tokens = len(self.tokens) - num_entity_tokens
        
        num_entities = len(ENTITIES) + len(self.entities)
        embed_size = num_other_tokens + num_entities + MAX_ARG_VALUES
        embedding = np.zeros((len(self.tokens), embed_size), dtype=np.float32)
        for token_id, token in enumerate(self.tokens):
            if '_' in token and token[0].isupper():
                continue
            embedding[token_id,token_id] = 1
        for i, entity in enumerate(ENTITIES):
            assert not np.any(embedding[:, num_other_tokens + i] > 0)
            for j in range(MAX_ARG_VALUES):
                token_id = self.dictionary[entity + '_' + str(j)]
                embedding[token_id, num_other_tokens + i] = 1
                embedding[token_id, num_other_tokens + num_entities + j] = 1
        for i, entity in enumerate(self.entities):
            assert not np.any(embedding[:, num_other_tokens + len(ENTITIES) + i] > 0)
            for j in range(MAX_ARG_VALUES):
                token_id = self.dictionary['GENERIC_ENTITY_' + entity + '_' + str(j)]
                embedding[token_id, num_other_tokens + len(ENTITIES) + i] = 1
                embedding[token_id, num_other_tokens + num_entities + j] = 1
        
        for i in range(len(embedding)):
            assert np.any(embedding[i] > 0)
        return embedding

    def dump_tokens(self):
        for token in self.tokens:
            print(token)

    def vectorize(self, program):
        seq = [None] * len(program)
        for i in range(len(program)):
            token = program[i]
            try:
                token_id = self.dictionary[token]
            except KeyError:
                raise ValueError("Unknown token " + token + " in " + (' '.join(program)))
            seq[i] = token_id
        return seq

    def parse(self, program):
        curr_state = self.start_state
        for token_id in program:
            next = self.transition_matrix[curr_state, token_id]
            if next == -1:
                raise ValueError("Unexpected token " + self.tokens[token_id] + " in " + (' '.join(self.tokens[x] for x in program)) + " (in state " + self.state_names[curr_state] + ")")
            #print("transition", self.state_names[curr_state], "->", self.state_names[next])
            curr_state = next
            
        if curr_state != self.end_state:
            raise ValueError("Premature end of program in " + (' '.join(self.tokens[x] for x in program)) + " (in state " + self.state_names[curr_state] + ")")

    def parse_all(self, fp):
        for line in fp.readlines():
            try:
                program = line.strip().split()
                program.append('<<EOS>>')
                self.parse(self.vectorize(program))
            except ValueError as e:
                print(e)

    def get_init_state(self, batch_size):
        return tf.ones((batch_size,), dtype=tf.int32) * self.start_state

    def constrain_logits(self, logits, curr_state):
        with tf.name_scope('constrain_logits'):
            allowed_tokens = tf.gather(tf.constant(self.allowed_token_matrix), curr_state)
            assert allowed_tokens.get_shape()[1:] == (self.output_size,)

            constrained_logits = logits - tf.to_float(tf.logical_not(allowed_tokens)) * 1e+10
        return constrained_logits

    def transition(self, curr_state, next_symbols, batch_size):
        with tf.name_scope('grammar_transition'):
            transitions = tf.gather(tf.constant(self.transition_matrix), curr_state)
            assert transitions.get_shape()[1:] == (self.output_size,)

            indices = tf.stack((tf.range(0, batch_size), next_symbols), axis=1)
            next_state = tf.gather_nd(transitions, indices)
            return next_state
    
    def _normalize_invocation(self, seq, start):
        if start >= len(seq):
            # truncated output
            return start
        assert self.tokens[seq[start]].startswith('tt:')
        assert not self.tokens[seq[start]].startswith('tt:param.')
        start += 1
        end = start
        
        params = []
        while end < len(seq) and self.tokens[seq[end]].startswith('tt:param.'):
            param_id = seq[end]
            end += 1
            if end >= len(seq):
                # truncated output
                return end
            operator = seq[end]
            end += 1
            if end >= len(seq):
                # this can occur at training time, if the output is truncated
                #raise AssertionError("missing value for " + self.tokens[param_id])
                params.append((param_id, operator, []))
                continue
            param_value = [seq[end]]
            end += 1
            while end < len(seq) and not self.tokens[seq[end]].startswith('tt:'):
                param_value.append(seq[end])
                end += 1
            params.append((param_id, operator, param_value))
        params.sort(key=lambda x: x[0])
        assert end <= len(seq)

        i = start
        for param_id, operator, param_value in params:
            seq[i] = param_id
            seq[i+1] = operator
            seq[i+2:i+2+len(param_value)] = param_value
            i += 2 + len(param_value)
            assert i <= end
        
        return end
    
    def _normalize_sequence(self, seq):
        i = 0
        if seq[0] == self.dictionary['rule']:
            i += 1
            i = self._normalize_invocation(seq, i)
            i = self._normalize_invocation(seq, i)
            if i < len(seq):
                i = self._normalize_invocation(seq, i)
        elif seq[0] in (self.dictionary['action'], self.dictionary['trigger'], self.dictionary['query']):
            self._normalize_invocation(seq, 1)
    
    def normalize_all(self, fp):
        for line in fp.readlines():
            try:
                program = line.strip().split()
                seq = self.vectorize(program)
                seq2 = list(seq)
                self._normalize_sequence(seq2)
                seq2.append(self.end)
                self.parse(seq2)
                seq2.pop()
                if seq != seq2:
                    print("was", ' '.join([self.tokens[x] for x in seq]))
                    print("now", ' '.join([self.tokens[x] for x in seq2]))
            except ValueError as e:
                print(e)
    
    def compare(self, seq1, seq2):
        seq1 = list(seq1)
        seq2 = list(seq2)
        #self._normalize_sequence(seq1)
        #self._normalize_sequence(seq2)
        return seq1 == seq2
        

if __name__ == '__main__':
    grammar = ThingtalkGrammar(sys.argv[1])
    #grammar.dump_tokens()
    #grammar.normalize_all(sys.stdin)
    grammar.parse_all(sys.stdin)
    #for i, name in enumerate(grammar.state_names):
    #    print i, name
