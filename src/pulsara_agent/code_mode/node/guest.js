// Evaluated only by QuickJS; potentially user-controlled getters/toJSON stay timed.
((invokeHost, prepareHost, discardHost, outputHost, names, valueLimit, diagnosticChars, decisionAvailable, runAgentFields, nameHintHost) => {
  'use strict';
  if (typeof decisionAvailable !== 'boolean') throw new TypeError('Code Mode Decision availability must be a boolean');
  const stringify = JSON.stringify, parse = JSON.parse, apply = Reflect.apply;
  const entries = Object.entries, keys = Object.keys, ownKeys = Reflect.ownKeys;
  const getPrototypeOf = Object.getPrototypeOf, create = Object.create;
  const setPrototypeOf = Object.setPrototypeOf, freeze = Object.freeze;
  const defineProperty = Object.defineProperty;
  const hasOwn = Object.hasOwn;
  const arrayIsArray = Array.isArray, numberFinite = Number.isFinite;
  const numberInteger = Number.isInteger, numberSafe = Number.isSafeInteger;
  const objectPrototype = Object.prototype;
  const asyncFunctionPrototype = getPrototypeOf(async function() {});
  const NativePromise = Promise, NativeSet = Set, NativeString = String, NativeNumber = Number;
  const NativeError = Error, NativeTypeError = TypeError, NativeRangeError = RangeError;
  const setHas = Set.prototype.has, setAdd = Set.prototype.add, setDelete = Set.prototype.delete;
  const codePointAt = String.prototype.codePointAt, slice = String.prototype.slice;
  const join = Array.prototype.join;
  const each = (array, callback) => {
    for(let index=0;index<array.length;index++) callback(array[index], index);
  };
  // ECMAScript has no UTF-8 encoder. Count before SDK toString copies across WASM;
  // lone surrogates become the same three-byte replacement character as in Node.
  function bounded(text) {
    let bytes = 0;
    for (let i=0;i<text.length;i++) {
      const code = apply(codePointAt, text, [i]);
      bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
      if(code > 0xffff) i++;
      if(bytes > valueLimit) throw new NativeRangeError('Code Mode bridge value boundary exceeded');
    }
    return text;
  }
  function argumentString(value) {
    for(let i=0;i<value.length;i++) {
      const code = apply(codePointAt,value,[i]);
      if(code === 0)
        throw new NativeTypeError('Tool argument UTF-8/canonical JSON storage rejects U+0000');
      if(code >= 0xd800 && code <= 0xdfff)
        throw new NativeTypeError('Tool arguments require valid Unicode strings representable as UTF-8');
      if(code > 0xffff) i++;
    }
    return value;
  }
  let preparing = false;
  let callbackFailure = false;
  function requireToolSubmission() {
    if(callbackFailure) throw new NativeTypeError('Asynchronous parameter callback invalidated this Cell tool submission');
    if(preparing) throw new NativeTypeError('Tool calls are forbidden in parameter functions');
  }
  function jsonClone(value, seen = new NativeSet()) {
    if(value === null || typeof value === 'boolean') return value;
    if(typeof value === 'string') return argumentString(value);
    if(typeof value === 'number') {
      if(!numberFinite(value) || (numberInteger(value) && !numberSafe(value)))
        throw new NativeTypeError('Bridge requires finite, safely representable JSON numbers');
      return value;
    }
    if(typeof value !== 'object') throw new NativeTypeError('Bridge requires strict JSON values');
    if(apply(setHas, seen, [value])) throw new NativeTypeError('Bridge rejects cyclic JSON values');
    const array = arrayIsArray(value);
    if(!array && getPrototypeOf(value) !== objectPrototype && getPrototypeOf(value) !== null)
      throw new NativeTypeError('Bridge rejects non-JSON objects');
    apply(setAdd, seen, [value]);
    const copy = array ? [] : create(null), length = array ? value.length : null;
    if(array) setPrototypeOf(copy, null);
    each(ownKeys(value), key => {
      if(typeof key !== 'string') throw new NativeTypeError('Bridge rejects Symbol keys');
      argumentString(key);
      if(array && key === 'length') return;
      const number = array ? NativeNumber(key) : 0;
      if(array && (!numberInteger(number) || number < 0 || NativeString(number) !== key || number >= length))
        throw new NativeTypeError('Bridge rejects array properties');
      copy[key] = jsonClone(value[key], seen);
    });
    if(array && keys(copy).length !== length) throw new NativeTypeError('Bridge rejects sparse arrays');
    apply(setDelete, seen, [value]);
    return copy;
  }
  const encode = value => bounded(stringify(jsonClone(value)));
  function render(value) {
    if(typeof value === 'string') return bounded(value);
    if(value === undefined) return 'undefined';
    const text = stringify(value);
    if(text === undefined) throw new NativeTypeError('Output value is not JSON serializable');
    return bounded(text);
  }
  function freezeTree(value) {
    if(value && typeof value === 'object') {
      each(keys(value), key => freezeTree(value[key]));
      freeze(value);
    }
    return value;
  }
  function call(name, parameters, preparation = null) {
    requireToolSubmission();
    return invokeHost(name, encode(parameters), preparation);
  }
  async function rejectUnknownName(kind, name) {
    requireToolSubmission();
    const label = kind === 'tool' ? 'tool' : 'run_agent assignment field';
    // Capture the calling source before awaiting the host's pure diagnostic.
    const error = new NativeTypeError(`Unknown ${label} ${stringify(name)}.`);
    const match = await nameHintHost(encode({kind, name}));
    if(match !== null) error.message += ` Did you mean ${stringify(match)}?`;
    throw error;
  }
  const tools = create(null);
  each(names, name => tools[name] = parameters => call(name, parameters));
  defineProperty(tools.decide, 'available', {value: freeze(() => decisionAvailable), enumerable: true});
  if(tools.run_agent) {
    if(!arrayIsArray(runAgentFields)) throw new NativeTypeError('Missing run_agent assignment fields');
    const allowedFields = new NativeSet(runAgentFields);
    tools.run_agent = function(assignments) {
      requireToolSubmission();
      if(!arrayIsArray(assignments) || assignments.length === 0)
        throw new NativeTypeError('run_agent requires a nonempty assignment array');
      // Capture the dense collection and ordinary JSON fields before the first await.
      // The catalog owns the field vocabulary; callbacks remain local to this VM.
      const length = assignments.length, indices = ownKeys(assignments);
      if(indices.length !== length + 1) throw new NativeTypeError('run_agent requires a dense array without extra properties');
      each(indices, key => {
        if(key === 'length') return;
        const index = typeof key === 'string' ? NativeNumber(key) : -1;
        if(!numberInteger(index) || index < 0 || NativeString(index) !== key || index >= length)
          throw new NativeTypeError('run_agent rejects array properties');
      });
      const captured = [], identities = [];
      setPrototypeOf(captured, null); setPrototypeOf(identities, null);
      for(let index=0; index<length; index++) {
        const member = assignments[index];
        if(member === null || typeof member !== 'object' || arrayIsArray(member)
            || (getPrototypeOf(member) !== objectPrototype && getPrototypeOf(member) !== null))
          throw new NativeTypeError('run_agent members must be configuration objects');
        const parameters = create(null);
        let invalidField;
        each(ownKeys(member), field => {
          if(invalidField !== undefined) return;
          if(typeof field !== 'string') throw new NativeTypeError('Unknown run_agent assignment field');
          if(!apply(setHas, allowedFields, [field])) { invalidField = field; return; }
          const value = member[field];
          if(typeof value === 'function' && (field === 'instruction' || field === 'depends_on')
              && getPrototypeOf(value) === asyncFunctionPrototype) {
            callbackFailure = true;
            throw new NativeTypeError('Parameter functions must return synchronously');
          }
          parameters[field] = typeof value === 'function' && (field === 'instruction' || field === 'depends_on')
            ? value : parse(encode(value));
        });
        if(invalidField !== undefined) return rejectUnknownName('assignment', invalidField);
        captured[index] = parameters;
        identities[index] = parameters.target === undefined ? {} : {target: parameters.target};
      }
      return (async () => {
        const prepared = await prepareHost(encode(identities));
        const refs = freezeTree(prepared.refs);
        try {
          const members = [];
          setPrototypeOf(members, null);
          each(captured, (member, index) => {
            const parameters = create(null);
            each(entries(member), pair => {
              const field = pair[0]; let value = pair[1];
              if(typeof value === 'function') {
                preparing = true;
                try {
                  value = value(refs);
                  if(value !== null && (typeof value === 'object' || typeof value === 'function')
                      && typeof value.then === 'function') {
                    // A rejected thenable can still run its own continuations. Seal
                    // new tool submissions in this Cell; admitted calls still settle.
                    callbackFailure = true;
                    throw new NativeTypeError('Parameter functions must return synchronously');
                  }
                  value = parse(encode(value));
                } finally { preparing = false; }
              }
              parameters[field] = value;
            });
            members[index] = {agent_id: refs.agents[index].agent_id,
              work_id: refs.works[index].work_id, parameters};
          });
          return await call('run_agent', {members}, prepared.request_id);
        } finally { discardHost(prepared.request_id); }
      })();
    };
  }
  each(names, name => freeze(tools[name]));
  freeze(tools);
  defineProperty(globalThis, 'tools', {value: new Proxy(tools, {get(target, name) {
    if(hasOwn(target, name)) return target[name];
    // Promise assimilation, JSON rendering and symbol introspection aren't tool lookups.
    if(typeof name !== 'string' || name === 'then' || name === 'toJSON') return undefined;
    return freeze(() => rejectUnknownName('tool', name));
  }})});
  defineProperty(globalThis, 'console', {value: freeze({log(...values) {
    const rendered = [];
    each(values, value => rendered[rendered.length] = render(value));
    outputHost('log', bounded(apply(join, rendered, [' '])));
  }})});
  return {
    render, parse,
    deferred: () => {
      let resolve, reject;
      const promise = new NativePromise((a,b) => {resolve=a;reject=b;});
      return {promise,resolve,reject};
    },
    error: value => {
      const error = new NativeError(value.message);
      error.name='ToolError';error.code=value.code;error.state=value.state;
      error.result=value.result;error.result_id=value.result_id;
      error.result_entry_id=value.result_entry_id;error.read=value.read;error.output_available=value.output_available;error.required_facts=value.required_facts;
      error.source_coverage=value.source_coverage;error.source_coverage_reason=value.source_coverage_reason;
      error.output_unavailability_reason=value.output_unavailability_reason;
      return error;
    },
    diagnose: value => {
      if(value !== null && (typeof value === 'object' || typeof value === 'function')) {
        const result = {name:NativeString(value.name ?? 'Error'),message:NativeString(value.message ?? value)};
        if(value.stack !== undefined) result.stack=NativeString(value.stack);
        if(value.name === 'ToolError') {
          result.code=value.code;result.state=value.state;result.result=value.result;
          result.read=value.read;result.output_available=value.output_available;result.required_facts=value.required_facts;
          result.source_coverage=value.source_coverage;result.source_coverage_reason=value.source_coverage_reason;
          result.output_unavailability_reason=value.output_unavailability_reason;result.result_id=value.result_id;
          result.result_entry_id=value.result_entry_id;
        }
        const encoded = stringify(result);
        if(encoded.length <= diagnosticChars) return encoded;
        const trimmed = {name:apply(slice,result.name,[0,80]),
          message:apply(slice,result.message,[0,diagnosticChars]),diagnostic_truncated:true};
        each(['code','state','result_id','result_entry_id','read','output_available','source_coverage','source_coverage_reason','output_unavailability_reason','required_facts'], field => {
          if(result[field] !== undefined) trimmed[field]=result[field];
        });
        let compact = stringify(trimmed);
        if(compact.length <= diagnosticChars + 1024) return compact;
        // Keep the accepted result's reading route when its business facts
        // exceed this existing diagnostic allowance. The source owns the facts.
        delete trimmed.required_facts;
        compact = stringify(trimmed);
        if(compact.length <= diagnosticChars + 1024) return compact;
        return stringify({name:trimmed.name,message:trimmed.message,diagnostic_truncated:true});
      }
      return stringify({name:'Error',message:apply(slice,NativeString(value),[0,diagnosticChars])});
    },
  };
})
