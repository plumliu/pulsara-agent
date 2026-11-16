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
  }
  return result;
}

export async function renderDeclarations(entries) {
  const rendered = {};
  for (const { name, schema } of entries) {
    rendered[name] = await compile(compilerSchema(schema), name, {
      bannerComment: '',
      unknownAny: true,
      // Bounds remain the argument validator's rules; avoid enumerating every
      // homogeneous array length in the model's structural declarations.
      maxItems: -1,
      unreachableDefinitions: false,
      style: { printWidth: 100, semi: true, singleQuote: false },
    });
  }
  return rendered;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  process.stdout.write(JSON.stringify(await renderDeclarations(JSON.parse(input))));
}
