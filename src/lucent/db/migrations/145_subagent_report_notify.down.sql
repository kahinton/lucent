DROP TRIGGER IF EXISTS subagent_report_request_notify ON requests;
DROP TRIGGER IF EXISTS subagent_report_task_notify ON tasks;
DROP FUNCTION IF EXISTS notify_subagent_report();
