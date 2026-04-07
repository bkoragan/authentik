"""Tests for django-dramatiq-postgres broker fixes related to empty/corrupt message payloads and requeue behavior. Issue #21410"""

from django.test import TestCase
from django_dramatiq_postgres.models import TaskState
from dramatiq import Message, actor, get_broker

from authentik.tasks.models import Task


class TestBrokerConsumeEmptyMessage(TestCase):
    """Test that the broker handles empty/corrupt message payloads gracefully"""

    def test_consume_empty_message_skips(self):
        """Verify _consume_one returns None for tasks with empty message payload
        instead of raising EOFError"""

        @actor
        def dummy_task():
            pass

        # Enqueue a task normally so it gets a valid DB row
        dummy_task.send()
        task = Task.objects.filter(actor_name=dummy_task.actor_name).first()
        self.assertIsNotNone(task)

        # Simulate the post-ack state: message emptied after DONE, then
        # somehow re-queued (the bug scenario)
        Task.objects.filter(message_id=task.message_id).update(
            message=b"",
            state=TaskState.QUEUED,
        )

        broker = get_broker()
        consumer = broker.consume("default")
        # _consume_one should gracefully skip the empty message
        result = consumer._consume_one(str(task.message_id))
        self.assertIsNone(result)

        # Task should be deleted entirely
        self.assertFalse(Task.objects.filter(message_id=task.message_id).exists())

        consumer.close()
        del broker.actors[dummy_task.actor_name]

    def test_consume_null_message_skips(self):
        """Verify _consume_one returns None for tasks with NULL message payload"""

        @actor
        def dummy_task_null():
            pass

        dummy_task_null.send()
        task = Task.objects.filter(actor_name=dummy_task_null.actor_name).first()
        self.assertIsNotNone(task)

        Task.objects.filter(message_id=task.message_id).update(
            message=None,
            state=TaskState.QUEUED,
        )

        broker = get_broker()
        consumer = broker.consume("default")
        result = consumer._consume_one(str(task.message_id))
        self.assertIsNone(result)

        # Task should be deleted entirely
        self.assertFalse(Task.objects.filter(message_id=task.message_id).exists())

        consumer.close()
        del broker.actors[dummy_task_null.actor_name]

    def test_consume_valid_message_works(self):
        """Verify _consume_one still works for valid messages"""

        @actor
        def dummy_task_valid():
            pass

        dummy_task_valid.send()
        task = Task.objects.filter(actor_name=dummy_task_valid.actor_name).first()
        self.assertIsNotNone(task)
        # Task should be in QUEUED state with a valid message
        self.assertEqual(task.state, TaskState.DONE)

        # Re-queue it with valid message so _consume_one can pick it up
        Task.objects.filter(message_id=task.message_id).update(
            state=TaskState.QUEUED,
        )

        broker = get_broker()
        consumer = broker.consume("default")
        result = consumer._consume_one(str(task.message_id))
        self.assertIsNotNone(result)

        consumer.close()
        del broker.actors[dummy_task_valid.actor_name]


class TestBrokerRequeue(TestCase):
    """Test that requeue preserves message content"""

    def test_requeue_preserves_message(self):
        """Verify requeue re-encodes the message so it can be consumed again"""

        @actor
        def dummy_task_requeue():
            pass

        dummy_task_requeue.send()
        task = Task.objects.filter(actor_name=dummy_task_requeue.actor_name).first()
        self.assertIsNotNone(task)
        original_message_id = str(task.message_id)

        # Simulate: task was acked (DONE) and message was emptied
        Task.objects.filter(message_id=task.message_id).update(
            message=b"",
            state=TaskState.DONE,
        )

        # Build an in-memory Message to requeue (as dramatiq would have it)
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

        # Task should be back to QUEUED with a non-empty message
        task.refresh_from_db()
        self.assertEqual(task.state, TaskState.QUEUED)
        self.assertTrue(len(task.message) > 0, "Message should be re-encoded after requeue")

        # Verify the re-encoded message is decodable
        decoded = Message.decode(bytes(task.message))
        self.assertEqual(decoded.actor_name, dummy_task_requeue.actor_name)

        consumer.close()
        del broker.actors[dummy_task_requeue.actor_name]

    def test_requeue_then_consume_succeeds(self):
        """End-to-end: requeue a task with empty message, then consume it successfully"""

        @actor
        def dummy_task_e2e():
            pass

        dummy_task_e2e.send()
        task = Task.objects.filter(actor_name=dummy_task_e2e.actor_name).first()
        self.assertIsNotNone(task)
        original_message_id = str(task.message_id)

        # Simulate ack emptying the message
        Task.objects.filter(message_id=task.message_id).update(
            message=b"",
            state=TaskState.DONE,
        )

        # Requeue with in-memory message
        msg = Message(
            queue_name="default",
            actor_name=dummy_task_e2e.actor_name,
            args=(),
            kwargs={},
            options={"message_id": original_message_id},
        )

        broker = get_broker()
        consumer = broker.consume("default")
        consumer.requeue([msg])

        # Now _consume_one should succeed (not raise EOFError)
        result = consumer._consume_one(original_message_id)
        self.assertIsNotNone(result)
        self.assertEqual(result.actor_name, dummy_task_e2e.actor_name)

        consumer.close()
        del broker.actors[dummy_task_e2e.actor_name]
