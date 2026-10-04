export const isFreePreview = (l: { include_in_preview?: number | boolean; is_complete?: boolean }) =>
	Boolean(l?.include_in_preview) && !l?.is_complete
