# DECISIONS.md

1. **Indexes.**
   I created a unique index on `employees.emp_code` and a compound index on `(department, joined_on)`. On `attendance_logs`, I created a unique index on `(emp_code, date)` to enforce daily punch uniqueness, `(date, -1, emp_code, 1)` for paginated attendance listings, `(emp_code, 1, punch_in, -1)` for fast punch-out matching, and `(date, 1, late_minutes, 1, emp_code, 1)` to provide a covered index scan for the leaderboard. I considered and rejected indexing `department` in `attendance_logs` because department belongs strictly to the employee entity; denormalizing it would duplicate mutable state.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously, both derive the same attendance date and execute atomic `insert_one` operations. MongoDB's unique index on `(emp_code, date)` accepts the first write and rejects the second with a `DuplicateKeyError`. My code catches this specific error and raises HTTP 409 Conflict, ensuring exactly one request gets 201.

3. **Ties.**
   Both tied employees are returned. My pipeline uses MongoDB's `$rank` window operator (standard competition ranking: 1, 2, 2, 4) and filters `rank <= limit` *after* ranking. Ties share the cutoff rank, ordered deterministically by `emp_code` ascending.

4. **Headcount.**
   The aggregation begins on the `employees` collection (filtering `joined_on <= period_end`), then performs a `$lookup` to join attendance records. Employees with zero logs produce an empty array but are preserved through `$unwind(preserveNullAndEmptyArrays=True)` and `$addToSet` on employee codes, ensuring their headcount is fully counted.

5. **One thing you would change for 100x data.**
   I would maintain incremental daily department rollups via MongoDB change streams or `$merge` during punch-out and regularizations, and shard `attendance_logs` by `(date, emp_code)` to avoid full-collection scans at multi-million record scale.
