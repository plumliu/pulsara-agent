// Evaluated only by QuickJS; potentially user-controlled getters/toJSON stay timed.
((invokeHost, prepareHost, discardHost, outputHost, names, valueLimit, diagnosticChars) => {
  'use strict';
  const stringify = JSON.stringify, parse = JSON.parse, apply = Reflect.apply;
  const entries = Object.entries, keys = Object.keys, ownKeys = Reflect.ownKeys;
  const getPrototypeOf = Object.getPrototypeOf, create = Object.create;
  const setPrototypeOf = Object.setPrototypeOf, freeze = Object.freeze;
  const defineProperty = Object.defineProperty;
  const arrayIsArray = Array.isArray, numberFinite = Number.isFinite;
  const numberInteger = Number.isInteger, numberSafe = Number.isSafeInteger;
  const objectPrototype = Object.prototype;
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
    if(preparing) throw new NativeTypeError('Tool calls are forbidden in batch parameter functions');
    return invokeHost(name, encode(parameters), preparation);
  }
  const tools = create(null);
  each(names, name => tools[name] = parameters => call(name, parameters));
  if(tools.run_agent) {
    tools.run_agent.batch = function(specs) {
      if(preparing) throw new NativeTypeError('Tool calls are forbidden in batch parameter functions');
      // Snapshot concrete fields before yielding for allocation. Internal iteration
      // uses captured intrinsics, so guest prototype changes cannot omit JSON fields.
      const captured = [];
      each(entries(specs), pair => {
        const key = pair[0], member = pair[1], parameters = create(null);
        each(entries(member), pair => {
          const field = pair[0], value = pair[1];
          parameters[field] = typeof value === 'function' && (field === 'instruction' || field === 'depends_on')
            ? value : parse(encode(value));
        });
        captured[captured.length] = [key, parameters];
      });
      const identities = create(null);
      each(captured, pair => {
        identities[pair[0]] = pair[1].target === undefined ? {} : {target: pair[1].target};
      });
      return (async () => {
        const prepared = await prepareHost(encode(identities));
        const peers = freezeTree(prepared.peers);
        try {
          const members = [];
          each(captured, pair => {
            const key = pair[0], parameters = create(null);
            each(entries(pair[1]), pair => {
              const field = pair[0]; let value = pair[1];
              if(typeof value === 'function') {
                preparing = true;
                try {
                  value = value(peers);
                  if(value !== null && (typeof value === 'object' || typeof value === 'function')
                      && typeof value.then === 'function')
                    throw new NativeTypeError('Batch parameter functions must return synchronously');
                  value = parse(encode(value));
                } finally { preparing = false; }
              }
              parameters[field] = value;
            });
            members[members.length] = {key, agent_id: peers[key].agent.agent_id,
              work_id: peers[key].work.work_id, parameters};
          });
          return await call('run_agent.batch', {members}, prepared.id);
        } finally { discardHost(prepared.id); }
      })();
    };
  }
  if(tools.wait_agent) tools.wait_agent.works = parameters => call('wait_agent.works', parameters);
  each(names, name => freeze(tools[name]));
  freeze(tools);
  defineProperty(globalThis, 'tools', {value: tools});
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
      error.result_entry_id=value.result_entry_id;error.artifact=value.artifact;
      return error;
    },
    diagnose: value => {
      if(value !== null && (typeof value === 'object' || typeof value === 'function')) {
        const result = {name:NativeString(value.name ?? 'Error'),message:NativeString(value.message ?? value)};
        if(value.stack !== undefined) result.stack=NativeString(value.stack);
        if(value.name === 'ToolError') {
          result.code=value.code;result.state=value.state;result.result=value.result;
          result.artifact=value.artifact;result.result_id=value.result_id;
          result.result_entry_id=value.result_entry_id;
        }
        const encoded = stringify(result);
        if(encoded.length <= diagnosticChars) return encoded;
        const trimmed = {name:apply(slice,result.name,[0,80]),
          message:apply(slice,result.message,[0,diagnosticChars]),diagnostic_truncated:true};
        each(['code','state','result_id','result_entry_id','artifact'], field => {
          if(result[field] !== undefined) trimmed[field]=result[field];
        });
        const compact = stringify(trimmed);
        if(compact.length <= diagnosticChars + 1024) return compact;
        return stringify({name:trimmed.name,message:trimmed.message,diagnostic_truncated:true});
      }
      return stringify({name:'Error',message:apply(slice,NativeString(value),[0,diagnosticChars])});
    },
  };
})
