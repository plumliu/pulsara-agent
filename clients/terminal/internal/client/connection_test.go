package client

import (
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestValidateLocalSocketRequiresOwnedPrivateUnixBoundary(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "pulsara-socket-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "client.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateLocalSocket(path); err != nil {
		t.Fatal(err)
	}

	if err := os.Chmod(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := validateLocalSocket(path); err == nil {
		t.Fatal("world-accessible terminal runtime directory was accepted")
	}
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}

	symlink := filepath.Join(root, "client-link.sock")
	if err := os.Symlink(path, symlink); err != nil {
		t.Fatal(err)
	}
	if err := validateLocalSocket(symlink); err == nil {
		t.Fatal("terminal socket symlink was accepted")
	}
}

func TestOpenConnectionCapturesKernelPeerAndSocketPathProof(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "pulsara-peer-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "peer.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	accepted := make(chan net.Conn, 1)
	go func() {
		connection, acceptErr := listener.Accept()
		if acceptErr == nil {
			accepted <- connection
		}
	}()
	connection, err := openConnection(path)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	peer, ownerUID, pathFingerprint, err := connection.peerIdentityParts()
	if err != nil {
		t.Fatal(err)
	}
	if peer.UID != uint64(os.Geteuid()) || ownerUID != peer.UID || pathFingerprint == "" {
		t.Fatalf("unexpected local peer proof: peer=%#v owner=%d path=%q", peer, ownerUID, pathFingerprint)
	}
	select {
	case server := <-accepted:
		_ = server.Close()
	case <-time.After(time.Second):
		t.Fatal("server did not accept the peer-proof connection")
	}
}
