import json

import frappe

from lms.lms.test_helpers import BaseTestUtils
from lms.lms.utils import get_quiz_with_questions


def _quiz_block_content(quiz):
	return json.dumps(
		{
			"time": 1765194986690,
			"blocks": [{"id": "q1", "type": "quiz", "data": {"quiz": quiz}}],
			"version": "2.29.0",
		}
	)


class TestQuizAuthorization(BaseTestUtils):
	"""get_quiz_with_questions must require enrollment/ownership, not just an LMS role."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		hash = frappe.generate_hash(length=6)
		cls.instructor = cls._create_user(
			f"qinstr-{hash}@example.com", "Ivy", "Instr", ["Course Creator", "Moderator"]
		)
		cls.enrolled = cls._create_user(f"qstud-{hash}@example.com", "Ed", "Enrolled", ["LMS Student"])
		cls.outsider = cls._create_user(f"qout-{hash}@example.com", "Ove", "Outsider", ["LMS Student"])

		cls.questions = cls._create_quiz_questions()
		cls.quiz = cls._create_quiz(cls.questions, title=f"Authz Quiz {hash}")
		cls.course = cls._create_course(title=f"Quiz Course {hash}", instructor=cls.instructor.email)
		cls.chapter = cls._create_chapter(f"QChapter {hash}", cls.course.name)
		# Link the quiz to the course the way production does: embed it as a content block,
		# which makes Course Lesson.save_lesson_details_in_quiz set LMS Quiz.course/lesson.
		# (Course Lesson.quiz_id is a manual field that is never auto-populated.)
		cls.lesson = cls._create_lesson(
			f"QLesson {hash}", cls.chapter.name, cls.course.name, _quiz_block_content(cls.quiz.name)
		)
		cls._create_enrollment(cls.enrolled.email, cls.course.name)

		# A second quiz never linked to any lesson or batch (e.g. mid-authoring).
		cls.unlinked_quiz = cls._create_quiz(cls.questions, title=f"Unlinked Quiz {hash}")

	def _call(self, user, quiz=None):
		frappe.session.user = user
		try:
			return get_quiz_with_questions(quiz or self.quiz.name)
		finally:
			frappe.session.user = "Administrator"

	def test_allowed_readers_can_read_the_quiz(self):
		cases = [
			("enrolled_student_linked_quiz", self.enrolled.email, None),
			("instructor_linked_quiz", self.instructor.email, None),
			# Regression: an author/moderator must still reach a quiz not yet
			# embedded anywhere.
			("moderator_unlinked_quiz", self.instructor.email, self.unlinked_quiz.name),
		]
		for case, user, quiz in cases:
			with self.subTest(case=case):
				result = self._call(user, quiz=quiz)
				self.assertEqual(len(result["questions_by_name"]), len(self.questions))

	def test_non_enrolled_user_cannot_read_the_quiz(self):
		cases = [
			("linked_quiz", None),
			("unlinked_quiz", self.unlinked_quiz.name),
		]
		for case, quiz in cases:
			with self.subTest(case=case):
				with self.assertRaises(frappe.PermissionError):
					self._call(self.outsider.email, quiz=quiz)


class TestFreePreviewQuizAccess(BaseTestUtils):
	"""The first module of every course is a free trial: a logged-in learner who is not
	enrolled may take the quiz embedded in a published course's preview lesson. Guests,
	drafts and non-preview lessons stay closed."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		hash = frappe.generate_hash(length=6)
		cls.instructor = cls._create_user(
			f"pvinstr-{hash}@example.com", "Pia", "Instr", ["Course Creator", "Moderator"]
		)
		cls.outsider = cls._create_user(f"pvout-{hash}@example.com", "Pat", "Outsider", ["LMS Student"])
		cls.member = cls._create_user(f"pvmem-{hash}@example.com", "Meg", "Member", ["LMS Student"])

		cls.questions = cls._create_quiz_questions()
		cls.quiz = cls._create_quiz(cls.questions, title=f"Preview Quiz {hash}")
		cls.course = cls._create_course(title=f"Preview Course {hash}", instructor=cls.instructor.email)
		cls.chapter = cls._create_chapter(f"PVChapter {hash}", cls.course.name)
		cls.lesson = cls._create_lesson(
			f"PVLesson {hash}", cls.chapter.name, cls.course.name, _quiz_block_content(cls.quiz.name)
		)
		# A second, non-preview lesson in the same course, used to move the quiz's
		# LMS Quiz.lesson pointer away from the preview lesson.
		cls.locked_lesson = cls._create_lesson(f"PVLesson Paid {hash}", cls.chapter.name, cls.course.name)
		frappe.db.set_value("Course Lesson", cls.lesson.name, "include_in_preview", 1)
		cls._create_enrollment(cls.member.email, cls.course.name)

		# Another published course with its own free preview lesson. Its lessons must
		# never open a quiz that belongs to the course above.
		cls.other_course = cls._create_course(
			title=f"Preview Other Course {hash}", instructor=cls.instructor.email
		)
		cls.other_chapter = cls._create_chapter(f"PVOChapter {hash}", cls.other_course.name)
		cls.other_lesson = cls._create_lesson(
			f"PVOLesson {hash}", cls.other_chapter.name, cls.other_course.name
		)
		frappe.db.set_value("Course Lesson", cls.other_lesson.name, "include_in_preview", 1)

	def _can(self, user, quiz=None):
		from lms.lms.permissions import can_access_quiz

		return can_access_quiz(quiz or self.quiz.name, user=user)

	def _call(self, user):
		frappe.set_user(user)
		try:
			return get_quiz_with_questions(self.quiz.name)
		finally:
			frappe.set_user("Administrator")

	def test_logged_in_non_enrolled_learner_can_read_a_free_preview_quiz(self):
		result = self._call(self.outsider.email)
		self.assertEqual(len(result["questions_by_name"]), len(self.questions))

	def _set_show_answers(self, value):
		frappe.db.set_value("LMS Quiz", self.quiz.name, "show_answers", value)
		frappe.clear_document_cache("LMS Quiz", self.quiz.name)

	@staticmethod
	def _answer_keys(row):
		return [key for key in row if key.startswith(("is_correct_", "possibility_"))]

	def test_preview_quiz_payload_carries_no_answer_key(self):
		# With live answers off, a preview learner who has not submitted gets no
		# correctness flags, no accepted answers and no explanations.
		self._set_show_answers(0)
		result = self._call(self.outsider.email)
		self.assertTrue(result["questions_by_name"])
		for row in result["questions_by_name"].values():
			self.assertEqual(self._answer_keys(row), [])
			self.assertEqual([key for key in row if key.startswith("explanation_")], [])

	def test_preview_payload_matches_an_enrolled_members_with_live_answers_on(self):
		# show_answers=1 ships explanations to every permitted reader; preview access
		# must get exactly what an enrolled member gets, and never the answer key.
		self._set_show_answers(1)
		preview = self._call(self.outsider.email)["questions_by_name"]
		enrolled = self._call(self.member.email)["questions_by_name"]
		self.assertEqual(set(preview), set(enrolled))
		self.assertTrue(preview)
		for name, row in preview.items():
			self.assertEqual(set(row), set(enrolled[name]))
			self.assertEqual(self._answer_keys(row), [])
			self.assertEqual(self._answer_keys(enrolled[name]), [])

	def test_guest_cannot_read_a_free_preview_quiz(self):
		# Even where the site lets guests browse preview lessons.
		frappe.db.set_single_value("LMS Settings", "allow_guest_access", 1)
		frappe.clear_cache(doctype="LMS Settings")
		self.assertFalse(self._can("Guest"))
		with self.assertRaises(frappe.PermissionError):
			self._call("Guest")

	def test_preview_lesson_of_an_unpublished_course_stays_denied(self):
		frappe.db.set_value("LMS Course", self.course.name, "published", 0)
		self.assertFalse(self._can(self.outsider.email))
		with self.assertRaises(frappe.PermissionError):
			self._call(self.outsider.email)

	def test_quiz_in_a_non_preview_lesson_stays_denied(self):
		frappe.db.set_value("Course Lesson", self.lesson.name, "include_in_preview", 0)
		self.assertFalse(self._can(self.outsider.email))
		with self.assertRaises(frappe.PermissionError):
			self._call(self.outsider.email)

	def test_preview_is_found_through_course_lesson_quiz_id(self):
		frappe.db.set_value("LMS Quiz", self.quiz.name, "lesson", None)
		frappe.db.set_value(
			"Course Lesson",
			self.lesson.name,
			{"content": None, "quiz_id": self.quiz.name},
		)
		self.assertTrue(self._can(self.outsider.email))

	def test_preview_is_found_through_a_lesson_that_embeds_the_quiz(self):
		# LMS Quiz.lesson records only the last lesson saved with the block. A quiz
		# embedded in a preview lesson and then in a paid one points at the paid one.
		frappe.db.set_value("LMS Quiz", self.quiz.name, "lesson", self.locked_lesson.name)
		self.assertTrue(self._can(self.outsider.email))

	def test_preview_embed_must_be_a_real_quiz_block(self):
		# The quiz name appearing as text in a preview lesson is not an embed.
		frappe.db.set_value("LMS Quiz", self.quiz.name, "lesson", self.locked_lesson.name)
		frappe.db.set_value(
			"Course Lesson",
			self.lesson.name,
			"content",
			json.dumps(
				{
					"time": 1765194986690,
					"blocks": [{"id": "m1", "type": "markdown", "data": {"text": self.quiz.name}}],
					"version": "2.29.0",
				}
			),
		)
		self.assertFalse(self._can(self.outsider.email))

	def test_quiz_with_no_owning_course_gets_no_preview_grant(self):
		# A quiz no course owns is not part of any free module.
		frappe.db.set_value("LMS Quiz", self.quiz.name, "course", None)
		self.assertFalse(self._can(self.outsider.email))

	def _move_preview_to_the_other_course(self):
		# The quiz's own preview lesson goes paid, so only the other course's preview
		# lesson could open it.
		frappe.db.set_value("Course Lesson", self.lesson.name, "include_in_preview", 0)

	def test_another_courses_preview_lesson_cannot_open_the_quiz_via_quiz_id(self):
		self._move_preview_to_the_other_course()
		frappe.db.set_value("Course Lesson", self.other_lesson.name, "quiz_id", self.quiz.name)
		self.assertFalse(self._can(self.outsider.email))
		with self.assertRaises(frappe.PermissionError):
			self._call(self.outsider.email)

	def test_another_courses_preview_lesson_cannot_open_the_quiz_via_an_embed(self):
		self._move_preview_to_the_other_course()
		# Written straight to the row: a lesson save now refuses the cross-course embed.
		frappe.db.set_value(
			"Course Lesson", self.other_lesson.name, "content", _quiz_block_content(self.quiz.name)
		)
		self.assertFalse(self._can(self.outsider.email))
		with self.assertRaises(frappe.PermissionError):
			self._call(self.outsider.email)


class TestLessonQuizCourseBinding(BaseTestUtils):
	"""A lesson may only name or embed a quiz of its own course (or an unowned one).
	Embedding course B's quiz in a course A lesson used to rewrite LMS Quiz.course and
	.lesson to course A, which hands course B's quiz to course A's learners."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		hash = frappe.generate_hash(length=6)
		cls.author = cls._create_user(f"qbauth-{hash}@example.com", "Bea", "Author", ["Course Creator"])
		cls.moderator = cls._create_user(f"qbmod-{hash}@example.com", "Max", "Mod", ["Moderator"])
		cls.questions = cls._create_quiz_questions()

		cls.course_a = cls._create_course(title=f"Binding Course A {hash}", instructor=cls.author.email)
		cls.chapter_a = cls._create_chapter(f"BAChapter {hash}", cls.course_a.name)
		cls.course_b = cls._create_course(title=f"Binding Course B {hash}", instructor=cls.author.email)
		cls.chapter_b = cls._create_chapter(f"BBChapter {hash}", cls.course_b.name)

		cls.quiz_b = cls._create_quiz(cls.questions, title=f"Binding Quiz B {hash}")
		cls.lesson_b = cls._create_lesson(
			f"BBLesson {hash}", cls.chapter_b.name, cls.course_b.name, _quiz_block_content(cls.quiz_b.name)
		)
		cls.lesson_a = cls._create_lesson(f"BALesson {hash}", cls.chapter_a.name, cls.course_a.name)

	def _save_as(self, user, **fields):
		frappe.set_user(user)
		try:
			lesson = frappe.get_doc("Course Lesson", self.lesson_a.name)
			lesson.update(fields)
			lesson.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")

	def test_quiz_b_is_owned_by_course_b(self):
		self.assertEqual(frappe.db.get_value("LMS Quiz", self.quiz_b.name, "course"), self.course_b.name)

	def test_non_moderator_cannot_set_another_courses_quiz_as_quiz_id(self):
		with self.assertRaises(frappe.ValidationError):
			self._save_as(self.author.email, quiz_id=self.quiz_b.name)

	def test_non_moderator_cannot_embed_another_courses_quiz(self):
		with self.assertRaises(frappe.ValidationError):
			self._save_as(self.author.email, content=_quiz_block_content(self.quiz_b.name))
		self.assertEqual(frappe.db.get_value("LMS Quiz", self.quiz_b.name, "course"), self.course_b.name)

	def test_non_moderator_cannot_embed_another_courses_quiz_in_instructor_content(self):
		with self.assertRaises(frappe.ValidationError):
			self._save_as(self.author.email, instructor_content=_quiz_block_content(self.quiz_b.name))

	def test_moderator_can_still_embed_another_courses_quiz(self):
		self._save_as(self.moderator.email, content=_quiz_block_content(self.quiz_b.name))
		self.assertEqual(frappe.db.get_value("LMS Quiz", self.quiz_b.name, "course"), self.course_a.name)

	def test_resaving_a_lesson_with_its_own_courses_quiz_is_allowed(self):
		# The seeded content: every quiz embedded only in its own course.
		frappe.set_user(self.author.email)
		try:
			lesson = frappe.get_doc("Course Lesson", self.lesson_b.name)
			lesson.quiz_id = self.quiz_b.name
			lesson.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("LMS Quiz", self.quiz_b.name, "lesson"), self.lesson_b.name)

	def test_an_unowned_quiz_can_be_embedded(self):
		unowned = self._create_quiz(self.questions, title=f"Binding Unowned {frappe.generate_hash(length=6)}")
		self._save_as(self.author.email, content=_quiz_block_content(unowned.name))
		self.assertEqual(frappe.db.get_value("LMS Quiz", unowned.name, "course"), self.course_a.name)
