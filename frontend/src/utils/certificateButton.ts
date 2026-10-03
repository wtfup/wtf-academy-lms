// Decides whether the course card shows "Get Certificate".
// Hidden once a certificate exists, so it never sits next to "View Certificate"
// (WTF audit finding #11: both buttons rendered after issuing).
type CourseLike = {
	enable_certification?: number | boolean
	membership?: { progress?: number } | null
}
type CertificationLike = { certificate?: unknown } | null | undefined

export function shouldShowGetCertificate(
	course: CourseLike | null | undefined,
	certification: CertificationLike
): boolean {
	if (!course?.enable_certification) return false
	if ((course.membership?.progress ?? 0) < 100) return false
	return !certification?.certificate
}

// One cache key for the course card and CertificationLinks so issuing a
// certificate flips both views at once, without a page reload.
export const certificationCacheKey = (course: string) => ['certificationDetails', course]
