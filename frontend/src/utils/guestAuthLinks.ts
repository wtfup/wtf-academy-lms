// Sign up / Log in links for the guest sidebar CTA (WTF audit findings #3/#6), and the
// single way every other "log in" / "sign up" exit in the SPA builds its URL.
// Only same-site paths are allowed as redirect targets (open-redirect guard).
export function guestAuthLinks(currentPath: string): { signup: string; login: string } {
	const safe = currentPath && currentPath.startsWith('/') && !currentPath.startsWith('//') ? currentPath : '/lms'
	const redirect = encodeURIComponent(safe)
	return {
		signup: `/signup?redirect-to=${redirect}`,
		login: `/login?redirect-to=${redirect}`,
	}
}

// The page the visitor is on, as a same-site path (with its query string).
export function currentPagePath(): string {
	if (typeof window === 'undefined') return ''
	return window.location.pathname + window.location.search
}

// "Log in" intents: back to `path` (default: the current page) after logging in.
export function loginUrl(path: string = currentPagePath()): string {
	return guestAuthLinks(path).login
}

// "Sign up / enroll" intents: back to `path` (default: the current page) after signing up.
export function signupUrl(path: string = currentPagePath()): string {
	return guestAuthLinks(path).signup
}
