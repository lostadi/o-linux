# Verification

These are bounded checks of the initial release, performed on 30 October 2026. They separate source tests, installed behavior, and boot observations. Installation state, keys, raw receipts, disk identities, and recovery data stay outside the repository.

The host was an Apple Silicon Mac. The persistent guest was Ubuntu 26.04.1 on AArch64. Local tests used Python 3.15 on macOS and Python 3.14 on Ubuntu.

The final suite passed **122 tests on macOS and 122 tests on Ubuntu**. The installed Mac application and `.O` integration matched all 16 corresponding repository source files byte-for-byte at the final drive check.

| Check | Result and scope |
| --- | --- |
| Existing Multipass guest adoption | Passed through this repository's installer. Existing disk retained; pinned command bridges verified. Fresh VM creation is covered by isolated tests, not a fresh live installation. |
| Literal arguments, streams and exit status | Passed through the installed Linux command and guest-to-Mac bridge, including spaces, quotes, empty arguments, dollar signs and newlines. stdout/stderr remained separate and exit status 7 was retained. |
| Terminal transport | Installed Linux command reported stdin, stdout and stderr as terminals in a real PTY. Redirected-stream behavior is also tested. |
| Persistence and automatic startup | Stopped the dedicated guest, then invoked a normal installed Linux command. Automatic startup, a persisted guest file, shared cwd, native launcher and host bridge passed. Startup plus follow-up validation took approximately 15.98 seconds; command stdout stayed valid JSON. |
| Read-only image bridge | Final installed source attached a disposable 1 MiB RAW image. Guest and host hashes matched; guest writes failed with EPERM; a separate server using production arguments rejected a writable NBD open. SIGINT returned zero, detached the mapping, closed both listeners and removed pending state. Source hash was unchanged. |
| Pending storage recovery | The installed `image --cleanup-pending` recovered a retained receipt without reopening its source. An earlier netlink fixture required a one-off exact-identity cleanup; no generic ownership bypass was added. |
| Automated remembered-drive opening | The installed `o-linux drive` evaluated the actual `read-drive.O`, decoded its typed OValue request, validated the nonce/profile binding, and completed the fixed native operation against the already mounted external ext4 drive. It matched physical and guest filesystem UUIDs, verified block/guest-filesystem/NFS-export/host-mount read-only state, reused the mount and saved an operation receipt. Fresh mount and close branches were tested with fixtures only. |
| Native Linux provider | Installed on actual Ubuntu with the same setup script. Native command execution, disk metadata inventory and unit tests passed. No native OSTADIX build on that guest is claimed. |
| Recorded O jobs | Plan-only did not dispatch the command. Host and Linux jobs passed source/intent admission and returned their command results. A host job preserved exit status 7. |
| OKernel | `o-linux kernel smoke`, using the existing O-core compiler and an isolated build directory, rebuilt and booted the kernel under QEMU. Serial evidence ended in `QEMU smoke: PASS`, including W^X, allocation/capability checks and CPL3 heartbeat. |
| Alpine through Linux | `o-linux systems --linux run linux-alpine-3.24.1-aarch64` passed the canonical laboratory's complete marker and cleanup checks in approximately 3.15 seconds of observed QEMU runtime. It reached its initramfs shell, answered uname, and powered down. |
| OpenBSD through Linux | `o-linux systems --linux run openbsd-7.9-amd64` passed the canonical laboratory's complete marker and cleanup checks in approximately 32.47 seconds of observed QEMU runtime. It reached the installation prompt. No installation was performed. |
| Python packaging | A wheel built successfully using the declared isolated setuptools build environment. A preliminary build without build isolation failed because setuptools was absent from the host interpreter. |

The Linux foreign-system checks used guest QEMU 10.2.1 and a persistent, Linux-local full source snapshot. The snapshot came from the selected OSTADIX checkout's committed tree; the selected harness and manifest bytes matched the active checkout. A local provenance file records the upstream commit and those hashes. The snapshot has its own local Git history so the harness can inspect a stable native filesystem. Host O planning and admission still use the host installation.

The default Mac laboratory path was exercised and failed: OpenBSD could not load firmware through `/dev/fd`, and Alpine produced no serial output before its 30-second timeout. The first Linux attempt against the large shared source tree also failed its Git-state inspection timeout. The explicit Linux route plus native source placement passed without modifying the upstream harness. Those results do not establish that the default Mac path works.

These boot checks establish the named QEMU observations. They do not establish persistent Alpine/OpenBSD installations, physical hardware support, KernelWorld governance, or replacement of the Ubuntu kernel. The OKernel doctor also reported missing optional Nix tooling; the baseline kernel smoke passed independently.

The final storage implementation was compared byte-for-byte with the installed version used for the successful fixture. Live tests found and corrected netlink's historical PID, Multipass empty-argument handling, and ioctl status output contaminating the JSON protocol before that final run.

Direct privileged execution from the O Python backend failed because the macOS sandbox does not permit sudo. The final design keeps that boundary: O emits the typed read-only request; the native caller validates it and owns privileged dispatch. Standalone `O read-drive.O` generates a request only. Actual runtime testing also corrected the typed OValue decoder and anylinuxfs's controlling-terminal and guest executable-path requirements. A bounded PTY helper now performs the guest metadata queries.

In the final installed rerun, sudo authorization occurred in the native caller's context before an isolated Python helper allocated the PTY. The complete O-request/native-open path returned `mounted: true` and `read_only: true`, preserved the existing mount identity, and saved a completed receipt. No global sudoers or sandbox setting was changed.

The native Mac bridge could list an existing read-only ext4 recovery mount. Direct access through the guest's shared `/Volumes` path returned permission denied in that installation; the host command bridge remained usable. The recovery source was not used for bridge write tests, repair, or bulk recovery scans.

See [the review record](audit.md) for defects found before publication and the boundaries that remain.
