package core

// Persister keeps one store's whole state under a name, as the bridge's KV
// bucket does (issue #151 step 3). Load returns nil, nil when the name holds
// nothing, so a first start begins empty as with a missing file.
//
// The file constructors stay: a registrar keeps its state in files, the bridge
// keeps it in the bucket, and the stores never know which.
type Persister interface {
	Load(name string) ([]byte, error)
	Save(name string, data []byte) error
}
