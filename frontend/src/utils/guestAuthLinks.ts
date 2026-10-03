// Sign up / Log in links for the guest sidebar CTA (WTF audit findings #3/#6).
// Only same-site paths are allowed as redirect targets (open-redirect guard).
export function guestAuthLinks(currentPath: string): { signup: string; login: string } {
	const safe = currentPath && currentPath.startsWith('/') && !currentPath.startsWith('//') ? currentPath : '/lms'
	const redirect = encodeURIComponent(safe)
	return {
		signup: `/login?redirect-to=${redirect}#signup`,
		login: `/login?redirect-to=${redirect}#login`,
	}
}
