package callout

import "testing"

// TestNameVector pins name() with one fixed vector; the MCP server's
// natsbus.agent_name (mcp/tests/test_natsbus.py) has the same vector, so both
// sides compute the same agent name from owner and host nkey.
func TestNameVector(t *testing.T) {
	const nkey = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NLCNF4"
	got := name("alice", nkey, "")
	if got != "7jdioaymoy77vybfzcvze3fc4u" {
		t.Fatalf("name = %q", got)
	}
	// A session part (#163): two sessions of one owner on one shared host key
	// are two names; the MCP server's vector is the same.
	if got := name("alice", nkey, "abcdefghijklmnopqrstuvwxyz"); got != "eadecaajdhhzsahypvqfemgypy" {
		t.Fatalf("name with session = %q", got)
	}
}
