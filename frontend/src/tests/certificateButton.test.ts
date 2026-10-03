import { describe, expect, it } from 'vitest'
import { shouldShowGetCertificate } from '@/utils/certificateButton'

// WTF audit finding #11: after a certificate was issued the course card showed BOTH
// "View Certificate" and "Get Certificate". "Get" must disappear once one exists.
describe('shouldShowGetCertificate', () => {
	const done = { enable_certification: 1, membership: { progress: 100 } }

	it('shows when certification is enabled, course complete and no certificate yet', () => {
		expect(shouldShowGetCertificate(done, { certificate: null })).toBe(true)
		expect(shouldShowGetCertificate(done, null)).toBe(true)
	})

	it('hides once a certificate already exists (View Certificate takes over)', () => {
		expect(shouldShowGetCertificate(done, { certificate: { name: 'abc' } })).toBe(false)
	})

	it('hides when the course is not complete', () => {
		expect(shouldShowGetCertificate({ enable_certification: 1, membership: { progress: 99 } }, null)).toBe(false)
		expect(shouldShowGetCertificate({ enable_certification: 1 }, null)).toBe(false)
	})

	it('hides when certification is disabled for the course', () => {
		expect(shouldShowGetCertificate({ enable_certification: 0, membership: { progress: 100 } }, null)).toBe(false)
	})
})
