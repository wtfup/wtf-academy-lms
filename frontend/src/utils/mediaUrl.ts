// Where an uploaded file's URL can point. Public media uploads are moved to S3 and
// served from the media CDN (lms.wtf_storage); everything else stays on /files/ or
// /private/files/. The one place that knows this, so call sites never re-derive it.
export const MEDIA_CDN_BASE = 'https://cdn.wtfgymsacademy.com/'

export function isUploadedMedia(url: string | null | undefined): boolean {
	const v = url || ''
	return (
		v.startsWith('/files/') ||
		v.startsWith('/private/files/') ||
		v.startsWith(MEDIA_CDN_BASE)
	)
}
