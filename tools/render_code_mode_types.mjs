/** Build model-readable declarations with the maintained JSON Schema compiler. */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { compile } = require('json-schema-to-typescript');

// The compiler owns type generation. This adapter expresses JSON's open values
// and the tuple vocabulary of our Draft 2020-12 schemas in its supported form.
function compilerSchema(schema) {
  if (schema === true || (schema && typeof schema === 'object'
      && !Array.isArray(schema) && Object.keys(schema).length === 0)) {
    return { tsType: 'JsonValue' };
  }
  if (schema === false || !schema || typeof schema !== 'object') return schema;
  if (Array.isArray(schema)) return schema.map(compilerSchema);
  if (!['type', 'const', 'enum', '$ref', 'oneOf', 'anyOf', 'allOf', 'tsType']
      .some(key => key in schema)) {
    return { ...schema, tsType: 'JsonValue' };
  }
  const result = {};
  for (const [key, value] of Object.entries(schema)) {
    if (key === 'properties' || key === '$defs' || key === 'definitions') {
      result[key] = Object.fromEntries(Object.entries(value).map(
        ([name, item]) => [name, compilerSchema(item)],
      ));
    } else if (key === 'additionalProperties' || key === 'items' || key === 'not'
        || key === 'if' || key === 'then' || key === 'else' || key === 'contains') {
      result[key] = compilerSchema(value);
    } else if (key === 'oneOf' || key === 'anyOf' || key === 'allOf'
        || key === 'prefixItems') {
      result[key] = value.map(compilerSchema);
    } else {
      result[key] = value;
    }
  }
  if (result.type === 'object' && !('additionalProperties' in result)) {
    result.additionalProperties = { tsType: 'JsonValue' };
  }
  if ('prefixItems' in result) {
    result.additionalItems = result.items ?? false;
    result.items = result.prefixItems;
    delete result.prefixItems;
  } else if (result.type === 'array' && result.minItems === result.maxItems
      && Number.isInteger(result.minItems) && result.minItems > 0
      && result.items && !Array.isArray(result.items)) {
    result.items = Array.from({ length: result.minItems }, () => result.items);
    result.additionalItems = false;
  }
  return result;
}

function mapChildren(schema, visit) {
  if (!schema || typeof schema !== 'object') return schema;
  const result = { ...schema };
  if (schema.properties) {
    result.properties = Object.fromEntries(Object.entries(schema.properties).map(
      ([key, value]) => [key, visit(value, key)],
    ));
  }
  for (const key of ['oneOf', 'anyOf', 'allOf']) {
    if (schema[key]) result[key] = schema[key].map(value => visit(value));
  }
  if (Array.isArray(schema.items)) {
    result.items = schema.items.map(value => visit(value, 'item'));
  } else if (schema.items) {
    result.items = visit(schema.items, 'item');
  }
  return result;
}

// Hoist identical record shapes within each page. Structural differences
// (including optional fields and annotations) keep their own declarations.
function shareRecords(definitions) {
  const records = new Map();
  const usedNames = new Set(['ApiDeclarations', 'JsonValue', ...Object.keys(definitions)]);
  const keyOf = value => JSON.stringify(value, (_, entry) => (
    entry && typeof entry === 'object' && !Array.isArray(entry)
      ? Object.fromEntries(Object.keys(entry).sort().map(key => [key, entry[key]]))
      : entry
  ));
  const pascal = value => value.replace(/(^|_)([a-z])/g, (_, __, letter) => letter.toUpperCase());
  function collect(value, field = 'record') {
    if (value?.type === 'object' && value.properties) {
      const key = keyOf(value);
      const record = records.get(key);
      if (record) {
        record.count += 1;
      } else {
        const { kind, type } = value.properties;
        const base = kind?.const
          ? pascal(kind.const.toLowerCase()) + 'Target'
          : type?.const ? pascal(type.const) + 'Part' : pascal(field);
        records.set(key, { value, base, count: 1 });
      }
    }
    mapChildren(value, (child, key) => collect(child, key === 'item' ? field : key ?? field));
    // A nonempty homogeneous array uses the item type both before and after
    // the spread in its TypeScript tuple.
    if (value?.minItems > 0 && value.items && !Array.isArray(value.items)) {
      collect(value.items, field);
    }
    return value;
  }
  for (const [name, definition] of Object.entries(definitions)) {
    mapChildren(definition, collect);
    if (definition.minItems > 0 && definition.items && !Array.isArray(definition.items)) {
      collect(definition.items, name.replace(/Args$/, 'Item'));
    }
  }
  for (const record of records.values()) {
    if (record.count < 2) continue;
    let alias = record.base;
    for (let suffix = 2; usedNames.has(alias); suffix += 1) alias = record.base + suffix;
    usedNames.add(alias);
    record.alias = alias;
  }
  function replace(value) {
    const alias = value?.type === 'object' && value.properties
      ? records.get(keyOf(value))?.alias
      : undefined;
    return alias ? { $ref: '#/definitions/' + alias } : mapChildren(value, replace);
  }
  const shared = Object.fromEntries(Object.entries(definitions).map(
    ([key, value]) => [key, mapChildren(value, replace)],
  ));
  for (const { alias, value } of records.values()) {
    if (alias) shared[alias] = mapChildren(value, replace);
  }
  return Object.fromEntries(Object.entries(shared).map(
    ([name, value]) => [name, { ...value, title: name }],
  ));
}

export async function renderDeclarations(entries) {
  const rendered = {};
  for (const { name, schema, definitions = {} } of entries) {
    const publicTypes = { [name]: schema, ...definitions };
    // Make each public type reachable, including unions. The compiler omits
    // unreferenced unions even when it emits unreferenced object definitions.
    const properties = Object.fromEntries(Object.keys(publicTypes).map(
      key => [key, { $ref: '#/definitions/' + key }],
    ));
    const input = compilerSchema({
      type: 'object', additionalProperties: false, properties, definitions: publicTypes,
    });
    input.definitions = shareRecords(input.definitions);
    const source = await compile(input, 'ApiDeclarations', {
      bannerComment: '',
      unknownAny: true,
      // Bounds remain the argument validator's rules; avoid enumerating every
      // homogeneous array length in the model's structural declarations.
      maxItems: -1,
      unreachableDefinitions: false,
      style: { printWidth: 100, semi: true, singleQuote: false },
    });
    rendered[name] = source.replace(/^export interface ApiDeclarations \{\n[\s\S]*?^\}\n/m, '').trim() + '\n';
  }
  return rendered;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  process.stdout.write(JSON.stringify(await renderDeclarations(JSON.parse(input))));
}
