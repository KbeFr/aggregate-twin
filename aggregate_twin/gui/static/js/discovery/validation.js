/**
 * Browser half of discovery_validation.py.
 *
 * The server sends each field's rules as [{rule, message, ...params}]; every rule
 * name here matches a Rule subclass there. The server re-checks on submit, so a
 * rule missing here only loses live feedback. To add one, register it in RULES
 * under the same name as the Python class.
 */

export const isBlank = value => value === null || value === undefined
  || (typeof value === 'string' && value.trim() === '');

const TYPE_CHECKS = {
  string: v => typeof v === 'string',
  number: v => typeof v === 'number' && Number.isFinite(v),
  boolean: v => typeof v === 'boolean',
  object: v => v !== null && typeof v === 'object' && !Array.isArray(v),
  list: v => Array.isArray(v),
  json: () => true,
};

const sizeOf = v => (typeof v === 'string' || Array.isArray(v) ? v.length : null);

export const RULES = {
  required: value => !isBlank(value),
  type: (value, rule) => (TYPE_CHECKS[rule.type] ?? TYPE_CHECKS.json)(value),
  pattern: (value, rule) => typeof value !== 'string' || new RegExp(`^(?:${rule.regex})$`).test(value),
  length: (value, rule) => {
    const size = sizeOf(value);
    return size === null || ((rule.min == null || size >= rule.min) && (rule.max == null || size <= rule.max));
  },
  range: (value, rule) => typeof value !== 'number'
    || ((rule.min == null || value >= rule.min) && (rule.max == null || value <= rule.max)),
  one_of: (value, rule) => (rule.options || []).some(option => option === value),
};

/** First failing rule's message, or null. A blank value only answers to `required`. */
export function validate(value, rules = []) {
  if (isBlank(value)) return rules.find(r => r.rule === 'required')?.message ?? null;
  for (const rule of rules) {
    const check = RULES[rule.rule];
    if (check && !check(value, rule)) return rule.message;
  }
  return null;
}

/** Custom text the operator typed, turned into the value it stands for. */
export function parseCustom(raw, type) {
  const text = raw ?? '';
  if (text.trim() === '') return { value: null, error: null };
  switch (type) {
    case 'string':
      return { value: text, error: null };
    case 'number': {
      const value = Number(text.trim());
      return Number.isFinite(value) ? { value, error: null } : { value: null, error: 'Expected a number.' };
    }
    case 'boolean':
      if (text === 'true' || text === 'false') return { value: text === 'true', error: null };
      return { value: null, error: 'Expected true or false.' };
    default:
      try {
        return { value: JSON.parse(text), error: null };
      } catch (e) {
        return { value: null, error: `Not valid JSON: ${e.message.replace(/^JSON\.parse: /, '')}` };
      }
  }
}

/** A value shown back as editable custom text. */
export function toRaw(value, type) {
  if (value === null || value === undefined) return '';
  if (type === 'string') return String(value);
  if (type === 'number' || type === 'boolean') return String(value);
  return JSON.stringify(value, null, 2);
}
