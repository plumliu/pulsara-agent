//go:build linux

package wire

import "golang.org/x/sys/unix"

func peerCredentialsFromFD(fd int) (PeerCredentials, error) {
	credential, err := unix.GetsockoptUcred(fd, unix.SOL_SOCKET, unix.SO_PEERCRED)
	if err != nil {
		return PeerCredentials{}, err
	}
	return PeerCredentials{
		UID:    uint64(credential.Uid),
		PID:    uint64(credential.Pid),
		HasPID: credential.Pid > 0,
	}, nil
}
