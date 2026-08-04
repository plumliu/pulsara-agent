//go:build darwin

package wire

import "golang.org/x/sys/unix"

func peerCredentialsFromFD(fd int) (PeerCredentials, error) {
	credential, err := unix.GetsockoptXucred(fd, unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
	if err != nil {
		return PeerCredentials{}, err
	}
	pid, pidErr := unix.GetsockoptInt(fd, unix.SOL_LOCAL, unix.LOCAL_PEERPID)
	if pidErr != nil {
		return PeerCredentials{UID: uint64(credential.Uid)}, nil
	}
	return PeerCredentials{UID: uint64(credential.Uid), PID: uint64(pid), HasPID: pid > 0}, nil
}
