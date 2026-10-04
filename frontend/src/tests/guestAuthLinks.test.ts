import { describe, expect, it } from 'vitest'
import { guestAuthLinks } from '@/utils/guestAuthLinks'

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
