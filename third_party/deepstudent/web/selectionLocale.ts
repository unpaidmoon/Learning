// The original functions only need a default label and one interpolation value.
// No application i18n store, browser event, task or source access is imported.
export function t(
  _key: string,
  options: { defaultValue: string; value?: string },
  _namespace?: string,
): string {
  return options.defaultValue.replaceAll('{{value}}', options.value ?? '');
}
