---
name: mcp-protocol-testing
description: 'Validate MCP tool implementations against protocol spec, test edge cases in memory operations, verify search behavior'
---

# MCP Protocol Testing

Validate MCP tool implementations against the protocol spec, test edge cases in memory operations, and verify search behavior.

## When to Use

- After adding or modifying MCP tool handlers in `src/lucent/tools/`
- When memory CRUD operations behave unexpectedly
- When search results are incorrect or incomplete
- When validating tag filtering, pagination, or error handling
- Before releasing a new version to ensure protocol compliance

## Key Files

- `src/lucent/tools/` — MCP tool implementations (memory create, read, update, delete, search)
- `tests/` — Test suite (run with `pytest`)
- `pyproject.toml` — Project configuration and test settings

## Testing Process

### Step 1: Memory CRUD Operations

Test the full lifecycle of memory objects:

1. **Create**: Verify a memory can be created with content, tags, and metadata. Confirm the response includes a valid ID and timestamps.
2. **Read**: Retrieve the created memory by ID. Verify all fields match what was stored.
3. **Update**: Modify content, tags, or metadata. Confirm the update is persisted and `updated_at` changes.
4. **Delete**: Remove the memory by ID. Confirm subsequent reads return a 404 or appropriate not-found response.

Edge cases to test:
- Create with empty content or missing required fields (expect validation error)
- Read with a non-existent ID (expect not-found response)
- Update a deleted memory (expect not-found response)
- Create with very large content or deeply nested metadata

### Step 2: Search Behavior

1. Create several memories with distinct content and tags
2. Test keyword search — verify results contain the search term
3. Test relevance ordering — more relevant results should rank higher
4. Verify that deleted memories do not appear in search results
5. Test search with no results — should return empty list, not an error

### Step 3: Tag Filtering

1. Create memories with various tag combinations
2. Filter by a single tag — verify only matching memories are returned
3. Filter by multiple tags — verify AND/OR behavior matches the spec
4. Filter by a tag that no memory has — expect empty results
5. Test tag operations: adding tags, removing tags, replacing tags

### Step 4: Pagination

1. Create enough memories to exceed a single page (check default page size)
2. Request the first page — verify correct count and presence of pagination metadata
3. Request subsequent pages — verify no duplicates and all items are eventually returned
4. Request a page beyond the last — expect empty results or appropriate response
5. Verify `total` count is accurate across paginated requests

### Step 5: Error Handling

1. Send malformed JSON — expect a clear error response, not a crash
2. Send requests with invalid field types (e.g., string where number expected)
3. Send requests with missing required fields
4. Verify error responses include useful error codes and messages
5. Confirm the server remains operational after handling errors (no state corruption)

### Step 6: Run the Test Suite

```bash
# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run only MCP-related tests
pytest tests/ -k "mcp" -v
```

## Best Practices

- Test each operation in isolation before testing combinations
- Always clean up test data to avoid polluting state across test runs
- Use fixtures for common setup (creating test memories, establishing connections)
- Check both the response body and HTTP status codes
- When a test fails, verify whether it is a test bug or an implementation bug before filing an issue
