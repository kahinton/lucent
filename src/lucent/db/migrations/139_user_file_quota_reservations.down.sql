-- Down migration for 139: remove in-flight quota reservations.

DROP TABLE IF EXISTS user_file_reservations;
