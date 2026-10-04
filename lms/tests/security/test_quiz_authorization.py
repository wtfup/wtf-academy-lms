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

	def test_preview_quiz_payload_carries_no_answer_key(self):
		# Preview access must expose nothing an enrolled learner would not get.
		result = self._call(self.outsider.email)
		for row in result["questions_by_name"].values():
			self.assertEqual([key for key in row if key.startswith("is_correct_")], [])
			self.assertEqual([key for key in row if key.startswith("possibility_")], [])
			self.assertEqual([key for key in row if key.startswith("explanation_")], [])

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
		frappe.db.set_value("LMS Quiz", self.quiz.name, {"course": None, "lesson": None})
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
