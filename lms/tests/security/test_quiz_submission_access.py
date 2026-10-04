import json

import frappe

from lms.lms.doctype.lms_quiz.lms_quiz import submit_quiz
from lms.lms.doctype.lms_quiz_submission.lms_quiz_submission import MaximumAttemptsExceededError
from lms.lms.test_helpers import BaseTestUtils


def _quiz_block_content(quiz):
	return json.dumps(
		{
			"time": 1765194986690,
			"blocks": [{"id": "q1", "type": "quiz", "data": {"quiz": quiz}}],
			"version": "2.29.0",
		}
	)


class TestQuizSubmissionAccess(BaseTestUtils):
	"""submit_quiz must require quiz access and enforce max_attempts
	(VULN-2026-FRAPPE-LMS-005)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		hash = frappe.generate_hash(length=6)
		cls.instructor = cls._create_user(
			f"sqinstr-{hash}@example.com", "Iris", "Instr", ["Course Creator", "Moderator"]
		)
		cls.enrolled = cls._create_user(f"sqenr-{hash}@example.com", "Ela", "Enrolled", ["LMS Student"])
		cls.outsider = cls._create_user(f"sqout-{hash}@example.com", "Otis", "Outsider", ["LMS Student"])

		cls.questions = cls._create_quiz_questions()
		cls.quiz = cls._create_quiz(cls.questions, title=f"Submit Quiz {hash}")
		cls.course = cls._create_course(title=f"Submit Course {hash}", instructor=cls.instructor.email)
		cls.chapter = cls._create_chapter(f"SQChapter {hash}", cls.course.name)
		# Embedding the quiz in a lesson makes save_lesson_details_in_quiz set
		# LMS Quiz.course/lesson, which is what can_access_quiz keys off.
		cls.lesson = cls._create_lesson(
			f"SQLesson {hash}", cls.chapter.name, cls.course.name, _quiz_block_content(cls.quiz.name)
		)
		cls._create_enrollment(cls.enrolled.email, cls.course.name)

		cls.results = [{"question_name": q.name, "answer": ["Option 1"]} for q in cls.questions]

	def _submit(self, user):
		frappe.session.user = user
		try:
			return submit_quiz(self.quiz.name, json.dumps(self.results))
		finally:
			frappe.session.user = "Administrator"

	def _cleanup_submissions(self, member):
		for name in frappe.get_all(
			"LMS Quiz Submission", {"quiz": self.quiz.name, "member": member}, pluck="name"
		):
			frappe.delete_doc("LMS Quiz Submission", name, force=True)

	def test_non_enrolled_user_cannot_submit(self):
		with self.assertRaises(frappe.PermissionError):
			self._submit(self.outsider.email)
		self.assertEqual(
			frappe.db.count("LMS Quiz Submission", {"quiz": self.quiz.name, "member": self.outsider.email}),
			0,
		)

	def test_enrolled_user_can_submit(self):
		result = self._submit(self.enrolled.email)
		self.assertIn("submission", result)
		self._cleanup_submissions(self.enrolled.email)

	def test_max_attempts_enforced(self):
		# max_attempts is enforced downstream by LMSQuizSubmission.validate; this guards
		# against that gate regressing (the score-oracle cap the access check relies on).
		self._cleanup_submissions(self.enrolled.email)
		frappe.db.set_value("LMS Quiz", self.quiz.name, "max_attempts", 1)
		try:
			self._submit(self.enrolled.email)
			with self.assertRaises(MaximumAttemptsExceededError):
				self._submit(self.enrolled.email)
		finally:
			frappe.db.set_value("LMS Quiz", self.quiz.name, "max_attempts", 0)
			self._cleanup_submissions(self.enrolled.email)


class TestFreePreviewQuizSubmission(BaseTestUtils):
	"""A logged-in learner who is not enrolled may submit the quiz of a free preview
	lesson and read the result. Progress is not written: it needs an enrollment."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		hash = frappe.generate_hash(length=6)
		cls.instructor = cls._create_user(
			f"pvsinstr-{hash}@example.com", "Ira", "Instr", ["Course Creator", "Moderator"]
		)
		cls.outsider = cls._create_user(f"pvsout-{hash}@example.com", "Ola", "Outsider", ["LMS Student"])

		cls.questions = cls._create_quiz_questions()
		cls.quiz = cls._create_quiz(cls.questions, title=f"Preview Submit Quiz {hash}")
		cls.course = cls._create_course(
			title=f"Preview Submit Course {hash}", instructor=cls.instructor.email
		)
		cls.chapter = cls._create_chapter(f"PVSChapter {hash}", cls.course.name)
		cls.lesson = cls._create_lesson(
			f"PVSLesson {hash}", cls.chapter.name, cls.course.name, _quiz_block_content(cls.quiz.name)
		)
		frappe.db.set_value("Course Lesson", cls.lesson.name, "include_in_preview", 1)
		cls.results = [{"question_name": q.name, "answer": ["Option 1"]} for q in cls.questions]

	def _as(self, user, fn, *args):
		frappe.set_user(user)
		try:
			return fn(*args)
		finally:
			frappe.set_user("Administrator")

	def test_preview_learner_can_submit_and_read_the_result(self):
		frappe.db.set_value("LMS Quiz", self.quiz.name, "passing_percentage", 0)
		result = self._as(self.outsider.email, submit_quiz, self.quiz.name, json.dumps(self.results))
		for key in ("submission", "score", "score_out_of", "percentage", "pass"):
			self.assertIn(key, result)
		self.assertEqual(
			frappe.db.get_value("LMS Quiz Submission", result["submission"], "member"),
			self.outsider.email,
		)
		# Progress needs an enrollment; submitting the free quiz neither enrols the
		# learner nor writes lesson progress.
		self.assertFalse(
			frappe.db.exists("LMS Enrollment", {"course": self.course.name, "member": self.outsider.email})
		)
		self.assertFalse(
			frappe.db.exists(
				"LMS Course Progress", {"lesson": self.lesson.name, "member": self.outsider.email}
			)
		)

	def test_preview_learner_cannot_submit_once_the_lesson_leaves_preview(self):
		frappe.db.set_value("Course Lesson", self.lesson.name, "include_in_preview", 0)
		with self.assertRaises(frappe.PermissionError):
			self._as(self.outsider.email, submit_quiz, self.quiz.name, json.dumps(self.results))

	def test_outsider_cannot_submit_when_the_course_is_unpublished(self):
		frappe.db.set_value("LMS Course", self.course.name, "published", 0)
		with self.assertRaises(frappe.PermissionError):
			self._as(self.outsider.email, submit_quiz, self.quiz.name, json.dumps(self.results))

	def test_outsider_cannot_submit_through_another_courses_preview_lesson(self):
		frappe.db.set_value("Course Lesson", self.lesson.name, "include_in_preview", 0)
		other_lesson = self._other_course_preview_lesson()
		frappe.db.set_value("Course Lesson", other_lesson, "quiz_id", self.quiz.name)
		with self.assertRaises(frappe.PermissionError):
			self._as(self.outsider.email, submit_quiz, self.quiz.name, json.dumps(self.results))

	def _other_course_preview_lesson(self):
		hash = frappe.generate_hash(length=6)
		course = self._create_course(title=f"Preview Submit Other {hash}", instructor=self.instructor.email)
		chapter = self._create_chapter(f"PVSOChapter {hash}", course.name)
		lesson = self._create_lesson(f"PVSOLesson {hash}", chapter.name, course.name)
		frappe.db.set_value("Course Lesson", lesson.name, "include_in_preview", 1)
		return lesson.name

	def test_guest_cannot_submit_a_preview_quiz(self):
		with self.assertRaises(frappe.PermissionError):
			self._as("Guest", submit_quiz, self.quiz.name, json.dumps(self.results))


class TestCheckAnswerAccess(BaseTestUtils):
	"""check_answer reports correctness, so it answers only to someone who may take the
	quiz: the same can_access_quiz that gates reading and submitting it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		hash = frappe.generate_hash(length=6)
		cls.instructor = cls._create_user(
			f"cainstr-{hash}@example.com", "Cai", "Instr", ["Course Creator", "Moderator"]
		)
		cls.enrolled = cls._create_user(f"caenr-{hash}@example.com", "Cal", "Enrolled", ["LMS Student"])
		cls.outsider = cls._create_user(f"caout-{hash}@example.com", "Cam", "Outsider", ["LMS Student"])

		cls.questions = cls._create_quiz_questions()
		cls.quiz = cls._create_quiz(cls.questions, title=f"Check Answer Quiz {hash}")
		cls.course = cls._create_course(title=f"Check Answer Course {hash}", instructor=cls.instructor.email)
		cls.chapter = cls._create_chapter(f"CAChapter {hash}", cls.course.name)
		cls.lesson = cls._create_lesson(
			f"CALesson {hash}", cls.chapter.name, cls.course.name, _quiz_block_content(cls.quiz.name)
		)
		cls._create_enrollment(cls.enrolled.email, cls.course.name)
		frappe.db.set_value("LMS Quiz", cls.quiz.name, "show_answers", 1)

	def _check(self, user):
		from lms.lms.doctype.lms_quiz.lms_quiz import check_answer

		question = self.questions[0]
		frappe.set_user(user)
		try:
			return check_answer(self.quiz.name, question.name, question.type, json.dumps(["Option 1"]))
		finally:
			frappe.set_user("Administrator")

	def test_enrolled_learner_can_check_an_answer(self):
		self.assertIsNotNone(self._check(self.enrolled.email))

	def test_staff_can_check_an_answer(self):
		self.assertIsNotNone(self._check(self.instructor.email))

	def test_outsider_cannot_check_an_answer_on_a_non_preview_quiz(self):
		with self.assertRaises(frappe.PermissionError):
			self._check(self.outsider.email)

	def test_preview_learner_can_check_an_answer(self):
		frappe.db.set_value("Course Lesson", self.lesson.name, "include_in_preview", 1)
		self.assertIsNotNone(self._check(self.outsider.email))

	def test_outsider_cannot_check_an_answer_when_the_course_is_unpublished(self):
		frappe.db.set_value("Course Lesson", self.lesson.name, "include_in_preview", 1)
		frappe.db.set_value("LMS Course", self.course.name, "published", 0)
		with self.assertRaises(frappe.PermissionError):
			self._check(self.outsider.email)

	def test_outsider_cannot_check_an_answer_through_another_courses_preview_lesson(self):
		hash = frappe.generate_hash(length=6)
		course = self._create_course(title=f"Check Answer Other {hash}", instructor=self.instructor.email)
		chapter = self._create_chapter(f"CAOChapter {hash}", course.name)
		lesson = self._create_lesson(f"CAOLesson {hash}", chapter.name, course.name)
		frappe.db.set_value(
			"Course Lesson", lesson.name, {"include_in_preview": 1, "quiz_id": self.quiz.name}
		)
		with self.assertRaises(frappe.PermissionError):
			self._check(self.outsider.email)
