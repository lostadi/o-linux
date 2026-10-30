# Review and corrections

This review covered the main command and O integration, installer/adoption, both SSH directions, native Linux setup, storage preservation and cleanup, and publication contents. Source review, isolated tests, and live integration checks were assessed separately.

| Finding | Correction |
| --- | --- |
| A successful NBD attachment could lose its ownership record after a timeout, failed client or malformed response. | Save host intent before attachment. Atomic guest receipts bind operation, instance, unique export, device, client PID and process start time. Reconcile the exact operation; retain unknown ownership. Cancellation tombstones prevent delayed attachment. |
| Storage SSH-key updates could truncate unrelated authorization entries. | Use an fsynced temporary file and atomic replacement, reject symlinks/nonregular files, and preserve unrelated bytes. Injected write failures leave the original intact. |
| A live netlink attachment outlived its recorded creator PID. | Use nbd-client's ioctl mode, which retains a live daemon, and wait for stable PID/start-time evidence. The implementation keeps PID-reuse protection. |
| Multipass dropped empty cleanup arguments. | Send explicit sentinels and decode them in the guest helper. |
| The ioctl disconnect command printed status into the helper's JSON protocol. | Capture subprocess stdout/stderr separately and test with simulated disconnect chatter. The final live lifecycle passed after the correction. |
| Interactive stdin could force a PTY despite redirected output. | Require all three streams to be terminals before selecting terminal transport. |
| A disabled `/Volumes` share still affected cwd selection. | Use saved sharing settings and fall back to guest home for unshared paths. |
| A timed-out command could have a completed top-level receipt; requested and effective directories were conflated. | Record completion as unknown after timeout, never retry automatically, and record both directories. |
| Installation could fail after partial mutations without a clear recovery record. | Preflight mount conflicts, record each phase and an exact retry command, retain backups, and test successful retries after injected failures. |
| Repeated setup discarded optional settings and reset omitted choices. | Preserve integration settings, resource values and sharing unless an explicit option overrides them. |
| Case-insensitive O/o names could select the wrong executable. | Prefer distinct native artifact names; avoid ambiguous Darwin aliases. Reject an invalid explicitly selected checkout instead of silently selecting another. |
| The job source hash did not enforce its planned execution identity. | Obtain the source-bound execution intent and supply both digests to native admission. Reject mismatches before dispatch. |
| Installed launchers bypassed the CLI error handler. | All launchers invoke the same entry function; an actual subprocess test checks bounded missing-configuration errors. |
| Local automation files could enter publication. | Ignore local history/state and inspect the staged source list explicitly. |
| A mount under a reused disk number could be adopted without binding its filesystem to the selected physical partition. | Compare the guest filesystem UUID with a bounded read-only signature from the currently selected partition before accepting or learning the identity. |
| Global anylinuxfs status confused unrelated mounts with the selected drive. | Scope conflict and completion checks to the selected device and mount path; leave unrelated instances running. |
| An uncertain mount attempt could be dispatched again, or discarded by selecting another drive. | Persist intent before starting, refuse redispatch and reselection while unresolved, and clear it only after positively verifying the exact selected mount. |
| Direct O backend execution could not run sudo on macOS. | Keep O contained: it emits a fixed typed request. The explicit native adapter validates exact fields, fresh nonce, installation and selection digest under the profile lock before one fixed read-only operation. |
| The initial adapter expected plain JSON instead of the runtime's typed OValue. | Decode only the required map of UTF-8 text values, then validate the exact request. Real O evaluation and malformed-value regression tests cover this boundary. |
| anylinuxfs guest inspection needed a controlling terminal and could not find blkid through its execution PATH. | Use a bounded literal-argument PTY helper and fixed guest executable paths. Local PTY tests and live read-only status/open verification passed. |
| Creating a new terminal before sudo could invalidate caller-scoped authorization caching. | Authorize in the native caller first; isolated Python then allocates the PTY. Keep fixed helper code, literal metadata arguments and bounded cleanup. The final installed existing-mount operation passed. |
| A completion-receipt write failure could mislabel native dispatch or damage its prior phase record. | Track dispatch separately, save phases atomically, preserve the last durable record after failed replacement, and never retry a native operation because recording failed. |

Regression tests cover these cases with actual helper execution against disposable fixtures where practical. The live storage test also exposed compatibility behavior absent from the fixtures; the final integration result belongs in [verification.md](verification.md).

The following remain deliberate limits or further work:

- Inventory reports native host metadata. General USB, GPU, camera and Bluetooth passthrough is not implemented. Host commands retain ordinary account permissions and macOS privacy controls.
- Physical-source metadata checks and a read-only export do not establish drive health or globally block other host writers. The physical-source bridge has not been qualified with the recovery disk.
- A process killed at an uncertain point can leave retained storage state. Receipt-based cleanup refuses to disconnect or kill a process without matching ownership evidence.
- Setup recovery is an explicit rerun path. Cross-host/guest rollback is not implemented.
- Native Linux installation was exercised on one Ubuntu AArch64 guest. Other distributions and architectures need their own qualification.
- Generated O documents and receipts bind source and execution intent. Installed Python modules, tools, configuration and external systems remain mutable dependencies. These are local records, not hermetic replay bundles or foreign-runtime lowering through O-core.
- Foreign boots require their own QEMU, firmware, artifact and source prerequisites. Listing a guest is not boot evidence. The documented default Mac failures remain distinct from successful Linux placement.
- Fresh guest creation, CI results and remote publication are separate evidence classes. Local adoption, tests or a wheel build do not establish those results.
