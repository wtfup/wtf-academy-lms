import { globSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { MEDIA_CDN_BASE, isUploadedMedia } from '@/utils/mediaUrl'

const SRC = resolve(__dirname, '..')

// Public media uploads now live on the media CDN (lms.wtf_storage), so an uploaded
// file's URL is either a local /files/ path or an absolute CDN URL.
describe('isUploadedMedia', () => {
	it('treats local public and private file paths as uploads', () => {
		expect(isUploadedMedia('/files/intro.mp4')).toBe(true)
		expect(isUploadedMedia('/private/files/intro.mp4')).toBe(true)
	})

	it('treats a media-CDN URL as an upload', () => {
		expect(MEDIA_CDN_BASE).toBe('https://cdn.wtfgymsacademy.com/')
		expect(isUploadedMedia('https://cdn.wtfgymsacademy.com/lms/0123abcd.mp4')).toBe(true)
	})

	it('treats links and look-alikes as links', () => {
		for (const v of [
			'',
			null,
			undefined,
			'h',
			'https://www.youtube.com/watch?v=htpg8CuD1Ec',
			'https://cdn.wtfgymsacademy.com.evil.example/x.mp4',
			'http://cdn.wtfgymsacademy.com/lms/x.mp4',
			'https://example.com/files/x.mp4',
		]) {
			expect(isUploadedMedia(v as string)).toBe(false)
		}
	})

	it('is the only place that decides what an uploaded file URL looks like', () => {
		const offenders = globSync('**/*.{ts,js,vue}', { cwd: SRC })
			.filter((f) => !f.startsWith('tests/') && f !== 'utils/mediaUrl.ts')
			.filter((f) =>
				/startsWith\(\s*['"`](\/private)?\/files\//.test(readFileSync(resolve(SRC, f), 'utf8'))
			)
		expect(offenders).toEqual([])
	})

	it('drives the video field upload/link switch', () => {
		const vue = readFileSync(resolve(SRC, 'components/Controls/VideoPreviewField.vue'), 'utf8')
		expect(vue).toMatch(/import \{ isUploadedMedia \} from '@\/utils\/mediaUrl'/)
		expect(vue).toMatch(/isUploadedMedia\(props\.modelValue\)/)
	})
})
