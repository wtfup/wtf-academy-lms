/**
 * Mirrors translate(): a message containing {0} returns a { format } object
 * rather than a string; any other message comes back unchanged. Shared so every
 * test that stubs `__` agrees with the real contract.
 */
export const tr = (s: string): any =>
	/{\d+}/.test(s)
		? {
				format: (...a: unknown[]) =>
					s.replace(/{(\d+)}/g, (_m, i) => String(a[Number(i)])),
			}
		: s
