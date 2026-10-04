import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { isFreePreview } from '../utils/freePreview'
import { guestAuthLinks } from '../utils/guestAuthLinks'

describe('FREE preview badge', () => {
	it('shows for preview lessons not yet completed', () => {
		expect(isFreePreview({ include_in_preview: 1 })).toBe(true)
		expect(isFreePreview({ include_in_preview: 0 })).toBe(false)
		expect(isFreePreview({ include_in_preview: 1, is_complete: true })).toBe(false)
		expect(isFreePreview({})).toBe(false)
	})

	it('is rendered by ChapterRow and the lesson sidebar', () => {
		for (const f of ['ChapterRow.vue', 'StudentLessonSidebar.vue']) {
			const src = fs.readFileSync(path.join(__dirname, '../components', f), 'utf8')
			expect(src).toContain('isFreePreview(lesson)')
			expect(src).toContain('wtf-free-chip')
		}
	})
})

describe('guest auth links go to the branded pages', () => {
	it('uses /signup and /login with the current path', () => {
		expect(guestAuthLinks('/lms/courses/x')).toEqual({
			signup: '/signup?redirect-to=%2Flms%2Fcourses%2Fx',
			login: '/login?redirect-to=%2Flms%2Fcourses%2Fx',
		})
	})
	it('rejects protocol-relative and empty paths', () => {
		expect(guestAuthLinks('//evil.com').signup).toBe('/signup?redirect-to=%2Flms')
		expect(guestAuthLinks('').login).toBe('/login?redirect-to=%2Flms')
	})
})
