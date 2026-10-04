import { globSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { currentPagePath, guestAuthLinks, loginUrl, signupUrl } from '@/utils/guestAuthLinks'

// WTF audit findings #3/#6: guests had no visible Sign up / Log in. The sidebar CTA must
// send them to the right form and bring them back to the page they were on.
describe('guestAuthLinks', () => {
	it('links signup and login with a redirect back to the current page', () => {
		const l = guestAuthLinks('/lms/courses/cpt-foundations-trial')
		expect(l.signup).toBe('/signup?redirect-to=%2Flms%2Fcourses%2Fcpt-foundations-trial')
		expect(l.login).toBe('/login?redirect-to=%2Flms%2Fcourses%2Fcpt-foundations-trial')
	})

	it('encodes query strings so they survive the round trip', () => {
		expect(guestAuthLinks('/lms/courses?category=Nutrition&x=1').signup).toBe(
			'/signup?redirect-to=%2Flms%2Fcourses%3Fcategory%3DNutrition%26x%3D1'
		)
	})

	it('never redirects off-site (open-redirect guard)', () => {
		expect(guestAuthLinks('https://evil.example.com/x').signup).toBe('/signup?redirect-to=%2Flms')
		expect(guestAuthLinks('//evil.example.com').login).toBe('/login?redirect-to=%2Flms')
	})

	it('falls back to the catalog when no path is known', () => {
		expect(guestAuthLinks('').login).toBe('/login?redirect-to=%2Flms')
	})
})

// Every "log in" / "sign up" exit in the SPA goes through guestAuthLinks, so the
// redirect back is always same-site and encoded (final review A7).
describe('loginUrl / signupUrl', () => {
	it('default to the page the visitor is on', () => {
		window.history.replaceState({}, '', '/lms/courses/x?tab=2')
		expect(currentPagePath()).toBe('/lms/courses/x?tab=2')
		expect(loginUrl()).toBe('/login?redirect-to=%2Flms%2Fcourses%2Fx%3Ftab%3D2')
		expect(signupUrl()).toBe('/signup?redirect-to=%2Flms%2Fcourses%2Fx%3Ftab%3D2')
	})

	it('accept an explicit same-site path and keep the open-redirect guard', () => {
		expect(loginUrl('/lms/batches/b1')).toBe('/login?redirect-to=%2Flms%2Fbatches%2Fb1')
		expect(signupUrl('//evil.example.com')).toBe('/signup?redirect-to=%2Flms')
	})
})

describe('raw /login links', () => {
	it('exist nowhere in frontend/src except guestAuthLinks.ts', () => {
		const SRC = resolve(__dirname, '..')
		const offenders = globSync('**/*.{ts,js,vue}', { cwd: SRC })
			.filter((f) => !f.startsWith('tests/') && f !== 'utils/guestAuthLinks.ts')
			.flatMap((f) =>
				readFileSync(resolve(SRC, f), 'utf8')
					.split('\n')
					.map((line, i) => ({ f, i: i + 1, line }))
					.filter(({ line }) => /['"`]\/login\b/.test(line))
					.map(({ f, i, line }) => `${f}:${i}: ${line.trim()}`)
			)
		expect(offenders).toEqual([])
	})
})
