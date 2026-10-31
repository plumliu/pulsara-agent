const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

// Only inspect the closed MCP records. Arbitrary env/header names do not gain
// protocol meaning by spelling themselves "withheld_fields".
export function hasWithheldMcpFields(value: unknown): boolean {
  const config = record(value);
  return [config, record(config.transport), record(config.auth), record(config.effect_policy), record(config.exposure_policy)]
    .some(item => Array.isArray(item.withheld_fields) && item.withheld_fields.length > 0);
}
