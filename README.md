# o-linux

I wanted Linux to stay available on my Mac. I wanted to type a command, work in the same files, and reach the host when a device or native tool needed macOS. I did not want to rebuild that arrangement every time I needed Linux.

`o-linux` keeps that arrangement in one terminal interface. On macOS, it uses a persistent Ubuntu VM managed by Multipass, shared directories, and authenticated command paths in both directions. On Linux, it uses the current system directly. The `linux` shortcut runs the same interface.

```sh
linux
linux python3 --version
linux exec uname -a
o-linux status
o-linux inventory usb
```

I also connect this interface to [OSTADIX](https://github.com/lostadi/OSTADIX): commands can become checked, planned `.O` documents with local execution receipts, and the existing kernel and foreign-system tools remain available through the same entry point.

| Capability | macOS host | Linux host |
| --- | --- | --- |
| Linux commands | Persistent Multipass guest, started when needed | Current system and account |
| Files | Host home at the same absolute path; `/Volumes` by default | Existing filesystem |
| Host commands | `mac COMMAND` from the guest, over a pinned SSH connection | Commands execute locally |
| Peripheral inventory | Native macOS metadata tools | Native Linux metadata tools, where installed |
| Raw image or external disk bridge | NBD export with a selected read/write mode | Use the host's native block-device access |
| OSTADIX jobs and system tools | Installed host OSTADIX checkout | Installed local OSTADIX checkout |

**Install on macOS.** The host needs Python 3.10 or newer, Multipass, QEMU's `qemu-nbd`, and OpenSSH. Install the prerequisites through your package manager; with Homebrew:

```sh
brew install python qemu
brew install --cask multipass
git clone https://github.com/lostadi/o-linux.git
cd o-linux
./setup.sh --plan
./setup.sh
```

Enable Remote Login for your account in macOS Settings before the actual setup. Setup checks that SSH is available and reads the host's public key locally; it does not enable the service or change sudoers. Run setup as your normal account.

The default guest is named `o-linux`, uses Ubuntu 26.04, and has 4 CPUs, 4 GB of memory, and a 16 GB disk. A fresh guest receives command-line, development, device-inspection, and recovery utilities through cloud-init. Resource flags apply when creating a VM; setup does not resize an existing VM.

```sh
./setup.sh --cpus 6 --memory 8G --disk 32G
./setup.sh --instance my-existing-linux --adopt-existing
./setup.sh --replace-launcher
./setup.sh --map-root
```

`--adopt-existing` explicitly selects an existing Multipass VM for configuration. Setup preserves its disk and checks for the required guest tools (`python3`, `nbd-client`, and `ddrescue`). It may start the guest, provision the command bridges, and add missing shared-directory mappings. An unrelated `linux` or `o-linux` launcher is preserved unless `--replace-launcher` is supplied; replacement creates a backup.

The host home and `/Volumes` are shared with the guest. These are working shares, so guest file changes affect the host. `--no-volumes` avoids adding a `/Volumes` mapping; it preserves any mapping already present. `--volumes` enables that mapping. On repeat setup, omitted resource and sharing flags retain the saved configuration.

`--map-root` adds persistent root-to-root UID/GID mappings within these two shares, alongside the normal account mappings. This permits access to root-owned entries that Multipass otherwise rejects even when their Unix permissions allow access. Setup preserves custom mappings, normally unmounts and recreates only the managed shares that need an update, and retains the full mapping plan if an update is interrupted. It does not share the host's root filesystem.

**Install on Linux.** Use the same checkout and setup command with Python 3.10 or newer:

```sh
git clone https://github.com/lostadi/o-linux.git
cd o-linux
./setup.sh --plan
./setup.sh
```

The native installer creates the command interface and local configuration. It does not create a VM, install packages, or provision a Mac bridge. Device access remains subject to the Linux account's permissions, installed drivers, and available tools. Use `--replace-launcher` if replacing an existing command is intentional.

Both installers place `linux` and `o-linux` in `~/.local/bin`. Add that directory to your shell's `PATH` if necessary. `pip install .` installs the `o-linux` Python entry point; the host configuration and bridges still require `./setup.sh` from the checkout.

**Work across the host and guest.** A command receives its literal argument list. Invoke a shell explicitly when shell syntax is needed:

```sh
linux python3 -c 'import os; print(os.getcwd())'
linux sh -c 'printf "%s\n" "$HOME"'
linux mac uname -s
linux mac --cwd /tmp -- ls -la
```

Inside the Mac's Linux guest, `mac COMMAND` runs a native host command as the configured Mac account. `mac --tty COMMAND` requests a terminal. Ordinary command execution keeps binary standard input/output and exit status; recorded jobs capture text output. Host and guest SSH identities are pinned in installation-local known-host files.

The shared home and `/Volumes` preserve the current directory when it is inside an enabled share. A Mac-side directory outside those shares falls back to the guest home for Linux execution. Inside the guest, `/host/home` and `/host/volumes` are convenient shortcuts.

**Use the hardware paths deliberately.** `o-linux inventory` reports metadata for disks, USB, Thunderbolt, Bluetooth, audio, cameras, displays, networking, printers, and serial ports. A category can be selected individually:

```sh
o-linux disks
o-linux inventory cameras
o-linux inventory serial
```

Inventory does not open cameras, record audio, print documents, open serial ports, mount filesystems, or repair disks. A device appearing in inventory establishes that the host reports it. It does not establish that Linux can drive it.

The Mac bridge runs with ordinary account permissions. macOS privacy permissions, including TCC, still apply. There is no general USB, GPU, camera, Bluetooth, or other peripheral passthrough. Devices remain usable through native host tools where those tools and permissions permit it; block storage has the explicit bridge below.

**Remember a Linux drive and its access mode.** On macOS, the drive command uses anylinuxfs to mount a selected Linux partition. New selections default to read-write; `--read-only` selects recovery-style access. Select the partition once from a current disk inventory; the saved identity is its partition UUID, so later opens locate the same partition even if macOS assigns a different disk number:

```text
o-linux drive select /dev/diskNsN --filesystem ext4 --read-write
o-linux drive
o-linux drive status
o-linux drive close
```

Install anylinuxfs separately using its Homebrew tap:

```sh
brew tap nohajc/anylinuxfs
brew trust nohajc/anylinuxfs
brew install anylinuxfs
```

Selection records metadata in the private `drives.json` file and does not mount the partition or require sudo. `--mountpoint /Volumes/Your-Drive` chooses the saved mount location; otherwise the default is `/Volumes/Linux-Drive`. Open, verification of an active mount, and close need macOS authorization. An interactive command requests it in the same Mac session when needed; a captured command fails clearly if authorization is unavailable.

Existing profiles retain their prior read-only mode. To change a saved drive, close its current mount, save the mode, then open it:

```sh
o-linux drive close
o-linux drive mode rw --mountpoint /Volumes/Linux-Drive
o-linux drive
```

Use `mode ro` to select read-only access. A mode change preserves the saved drive identity and requires its mount and pending operations to be cleared. An exact owned inactive reader can be closed after disconnection, and the new mode can then be saved for the next connection. Status distinguishes a disconnected selection from a mounted drive.

The same `o-linux drive` commands work inside the provisioned Ubuntu guest. They use the pinned Mac bridge and the Mac's saved selection. A host-only dispatch guard rejects a wrongly configured non-Mac destination. Standalone Linux installations without this bridge cannot use the anylinuxfs reader.

`lsblk` in Ubuntu lists the guest's block devices. A drive opened by this filesystem workflow remains mounted on the Mac and does not become a guest block device. Use the shared `/Volumes` path for normal file access after configuring its ID mappings, or use the host command bridge:

```sh
ls /Volumes/Linux-Drive
mac ls /Volumes/Linux-Drive
mac cat /Volumes/Linux-Drive/path/to/file > recovered-file
```

The second command writes a copy into the guest's current directory. Host permissions still apply to the source.

`o-linux drive` and `o-linux drive open` evaluate the installed `.O` document to obtain a typed request bound to the saved selection, access mode and invocation nonce. The native host adapter validates that request before performing the fixed open operation. Read-write mode requires a writable block device, ext4 `rw` with journal handling enabled, a writable NFS export and a writable host mount. Read-only mode requires a read-only block device, ext4 `ro,noload` and read-only NFS layers. An already-active matching mount is reused only after its mode and physical filesystem identity are verified.

After a disconnect, macOS may assign the same partition a new disk number while its old mount remains. Status reports an identified stale mount without changing it. Open can retire that mount and reopen the selected partition only when the old devices are absent or positively identified as inactive, the exact saved mount receipt still matches, and the current physical filesystem has the saved UUID. Retained device nodes must match inactive IOMedia records for the saved partition and whole-disk sizes and be absent from the active disk inventory. Teardown intent is saved before normal unmount; uncertain completion is retained and never triggers repeated teardown. Conflicting or unowned mounts are left untouched.

The same saved selection has a fixed `.O` entry point installed with the application. To inspect its structured request without mounting anything, invoke it directly with the absolute path to your installed OSTADIX backends:

```sh
O "$HOME/.local/share/o-linux/app/integrations/ostadix/read-drive.O" /absolute/path/to/OSTADIX/backends
```

The document calls `drive_frontend.open_request()` through the Python backend and returns a structured O value. It performs no mount or sudo operation. `o-linux drive` provides the native adapter that carries out the validated request. This boundary is necessary on macOS: the sandboxed backend cannot execute sudo even with O's ordinary process authority. The workflow does not disable the sandbox or route privilege through an SSH workaround.

`O_LINUX_STATE` selects the installation containing the module and saved drive identity. The request binds the selected profile's digest and mode, and the host adapter checks them again before dispatch. A source digest alone would not bind the environment or saved configuration. This request-and-adapter workflow is separate from the checked, planned, receipt-producing `o-linux job` workflow below. Read-write mode permits normal filesystem changes and journal replay; mounting does not run a filesystem repair or recover damaged media.

The adapter records its own declaration and native-operation receipt under `runs/drive-*` in the private state directory. A fresh nonce binds the returned request to that invocation. The saved-profile lock covers declaration, validation, and native dispatch. Failure after native dispatch is recorded as unknown completion rather than a rejected declaration; an uncertain mount is retained for reconciliation instead of automatically launched again.

For an existing raw disk image on macOS:

```sh
o-linux image /path/to/image.raw --read-write
```

For a physical disk, first inspect a current inventory and obtain its exact byte size. Then substitute that exact whole-disk path and size:

```text
o-linux disk /dev/diskN --expected-size EXACT_BYTE_COUNT --read-write
```

The physical bridge accepts an external, physical, whole disk with no mounted partitions and rejects unsupported compound layouts and active anylinuxfs readers. Run `sudo -v` first if the device requires privileged access. `--read-write` and `--read-only` select the source descriptor, NBD server and guest block-device mode together. Without a flag, the private `storage_access_mode` configuration selects the mode; existing configurations default to `ro`. The command prints the allocated guest device and stays in the foreground. It mounts no filesystem. Unmount any guest filesystem before closing a writable bridge; its cleanup flushes the owned device before disconnecting.

Read-write exports allow guest writes to the selected source. Read-only exports block writes through that export. Neither mode globally excludes independent host processes or establishes a drive's health. Disk recovery and filesystem operations remain separate work.

If a connection or cleanup operation has an unknown result, the bridge retains its ownership record. `o-linux image --cleanup-pending` reconciles that record against the guest receipt without reopening the source. It does not guess ownership or discard an unresolved mapping to make room for another one.

**Use OSTADIX.** Install OSTADIX separately and set `O_LANG_ROOT` to that checkout, or set `ostadix_root` in the local configuration. The resolver also checks the conventional `~/ostadix-BM` and `~/OSTADIX` directories.

```sh
export O_LANG_ROOT=/path/to/OSTADIX
o-linux job --plan -- python3 --version
o-linux job -- python3 -c 'print("Linux through O")'
o-linux job --target host -- uname -s
o-linux ostadix doctor
o-linux kernel help
o-linux systems list
o-linux systems --linux list
```

A job writes an `.O` document, checks it, asks the native `o` planner for an execution intent, and verifies that the plan binds the document's SHA-256 digest. Execution supplies both the source digest and the execution-intent digest to the O evaluator. The document uses the Python backend to dispatch the selected native or SSH command. `--plan` records the document and plan without dispatching that command.

Receipts include the source, plan, command result, and execution status under the private state directory. These are local execution records. They are not a hermetic dependency closure or evidence that a foreign runtime was compiled through O-core. A timed-out command may still have remote effects; an unknown completion is recorded and is not retried automatically.

`o-linux kernel` routes to `o tool kernel`; `o-linux systems` routes to `o tool foreign-kernel-lab`. Their build, QEMU, download, and physical-media requirements remain those of the installed OSTADIX tools. These routes do not replace the Ubuntu guest's kernel. Generated kernel artifacts default to the o-linux state directory unless `OCORE_BUILD_DIR` is already set.

`o-linux systems --linux ARGS...` explicitly runs the installed OSTADIX foreign-system harness in Linux. It creates the same checked, planned, digest-bound O job used by other recorded commands, then dispatches `python3` and the harness's literal arguments through the Linux provider. By default, a Mac's OSTADIX checkout must be in an enabled shared path. This keeps O orchestration on the host while the harness and its QEMU processes run in Linux.

For repeated system runs, I recommend a full OSTADIX checkout or a provenance-recorded source snapshot on the Linux filesystem. The harness inspects repository state, and a large shared worktree can exceed its Git-query timeout. Select that Linux-native source directory explicitly:

```sh
o-linux systems --linux --linux-root /home/ubuntu/OSTADIX list
```

Set `linux_ostadix_root` to the absolute Linux path in the local `config.json` to retain that choice. `--linux-root` overrides the saved value and is consumed by o-linux. It does not change the host `O_LANG_ROOT` used to check and plan the job. The directory must contain the real harness and manifest, with valid repository or source-release provenance; o-linux does not synthesize upstream provenance or patch the harness. Guest-only paths are evaluated in Linux rather than checked against the Mac filesystem.

The Linux systems route defaults its guest cache to the shared host home at `~/.local/share/ostadix/guests`, so the harness can verify and reuse downloaded guest artifacts. `run` and `run-all` default their output to the shared o-linux state's `foreign-system-runs` directory. Explicit `--guest-dir` and `--output-dir` arguments are preserved; they must name paths accessible to the selected Linux provider. Selecting the Linux route does not itself establish that a guest booted; the harness's observations and the verification record establish that separately.

Fresh Ubuntu guests include the ARM and x86 QEMU system emulators plus AArch64 UEFI and OVMF firmware. Adoption leaves package installation explicit. On an existing Ubuntu or Debian guest, install these once:

```sh
linux sudo apt-get update
linux sudo apt-get install -y --no-install-recommends qemu-system-arm qemu-system-x86 qemu-efi-aarch64 ovmf
```

Those same commands use the current system with the native Linux provider. For other distributions, install the corresponding ARM/x86 QEMU system emulators and firmware through that distribution's package manager. The native installer does not install them automatically. A selected foreign-system manifest may require additional tools or specific firmware; its checks determine whether that system can run.

**Keep installation state local.** The default state directory is `~/.local/share/o-linux`. `O_LINUX_STATE=/another/path` selects a separate installation. Configuration, SSH keys, known-host files, installed source, backups, storage state, and job receipts belong there rather than in this repository. An existing configuration for a different provider or account is rejected instead of being converted implicitly.

Setup records its current phase in `install-transaction.json`. A failure can leave part of the installation applied. It retains changes and backups, records the failed phase, and prints the exact retry command. Rerun that command to complete the idempotent setup. This is a retry path, not an automatic rollback; an interrupted process can leave an incomplete transaction without a final error message.

I keep the mechanism and its verification separate. [Architecture](docs/architecture.md) explains the boundaries; [verification](docs/verification.md) records what was actually exercised.

To run the unit tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The test suite uses temporary files and fake commands for installer and VM operations. Running it does not require a cloud account, a VM, attached storage, or an OSTADIX build. Live integration checks are recorded separately.

Copyright © 2026 Lee Daghlar Ostadi. [ISC license](LICENSE).
