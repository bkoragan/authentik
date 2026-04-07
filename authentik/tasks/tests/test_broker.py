"""Tests for django-dramatiq-postgres broker fixes related to empty/corrupt message payloads and requeue behavior. Issue #21410"""

from django.test import TestCase
from django_dramatiq_postgres.models import TaskState
from dramatiq import Message, actor, get_broker

from authentik.tasks.models import Task


class TestBrokerConsumeEmptyMessage(TestCase):
    """Test that the broker handles empty/corrupt message payloads gracefully"""

    def test_ack_empties_message(self):
        """Verify that acking a task sets message to empty bytes"""

        @actor
        def dummy_task_ack():
            pass

        dummy_task_ack.send()
        task = Task.objects.filter(actor_name=dummy_task_ack.actor_name).first()
        self.assertIsNotNone(task)
        # After send(), the task is synchronously executed and acked (DONE)
        self.assertEqual(task.state, TaskState.DONE)
        # The _post_process_message optimization empties the message
        self.assertEqual(task.message, b"")

        broker = get_broker()
        del broker.actors[dummy_task_ack.actor_name]

    def test_empty_message_not_decodable(self):
        """Confirm that decoding an empty message raises an error,
        demonstrating why the guard in _consume_one is needed"""

        with self.assertRaises(Exception):
            Message.decode(b"")

    def test_null_message_not_decodable(self):
        """Confirm that decoding a None message raises an error"""

        with self.assertRaises(Exception):
            Message.decode(b"\x00")


class TestBrokerRequeue(TestCase):
    """Test that requeue re-encodes the message so it remains decodable"""

    def test_requeue_preserves_message(self):
        """Verify requeue re-encodes the message so it can be consumed again"""

        @actor
        def dummy_task_requeue():
            pass

        dummy_task_requeue.send()
        task = Task.objects.filter(actor_name=dummy_task_requeue.actor_name).first()
        self.assertIsNotNone(task)
        original_message_id = str(task.message_id)

        # After ack, message is emptied
        self.assertEqual(task.message, b"")
        self.assertEqual(task.state, TaskState.DONE)

        # Build an in-memory Message to requeue (as dramatiq Retries middleware would)
        msg = Message(
            queue_name="default",
            actor_name=dummy_task_requeue.actor_name,
            args=(),
            kwargs={},
            options={"message_id": original_message_id},
        )

        broker = get_broker()
        consumer = broker.consume("default")
        consumer.requeue([msg])

        # Task should be back to QUEUED with a non-empty, decodable message
        task.refresh_from_db()
        self.assertEqual(task.state, TaskState.QUEUED)
        self.assertTrue(len(task.message) > 0, "Message should be re-encoded after requeue")

        # Verify the re-encoded message is decodable
        decoded = Message.decode(bytes(task.message))
        self.assertEqual(decoded.actor_name, dummy_task_requeue.actor_name)

        consumer.close()
        del broker.actors[dummy_task_requeue.actor_name]
