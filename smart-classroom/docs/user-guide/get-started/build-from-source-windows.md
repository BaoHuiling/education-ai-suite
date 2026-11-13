# Build the Installer from Source

Most users download the installer from the
[Releases page](https://github.com/open-edge-platform/education-ai-suite/releases); see
[Get Started on Windows](./get-started-windows.md). This guide is for users who want to
build `SmartClassroom-<version>-setup.exe` themselves, for example from a branch or
with local changes. The installer you build is installed and used the same way as a
released one.

> [!WARNING]
> Running Smart Classroom from a source checkout with `setup-smart-classroom.ps1` and
  the browser UI (`start-smart-classroom.ps1 -WebUI`) is deprecated and will be removed
  after release 2027.0.0. Install the desktop app instead. The installed app keeps its
  data in `%LOCALAPPDATA%\SmartClassroom`, so models and settings from a source checkout
  are not reused.

## Prerequisites

- **OS**: Windows 11
- **Git**
- **Node.js**: 20.19 or later 20.x, or 22.12 or later

If you are behind a proxy, set `HTTP_PROXY` and `HTTPS_PROXY` in the shell you build
from. `npm install` and the Electron download made by electron-builder both use them.

## Step 1: Clone the Repository

Go to the target directory of your choice and clone the suite.
If you want to clone a specific release branch, replace `main` with the desired tag.
To learn more about partial cloning, see the [Repository Cloning guide](https://docs.openedgeplatform.intel.com/dev/OEP-articles/contribution-guide.html#repository-cloning-partial-cloning).

```bash
git clone --filter=blob:none --sparse --branch main https://github.com/open-edge-platform/education-ai-suite.git
cd education-ai-suite
git sparse-checkout set smart-classroom/Windows
```

## Step 2: Build the Installer

```powershell
cd smart-classroom\Windows\ui
npm install
npm run electron:build
```

The installer is written to `smart-classroom\Windows\ui\release\SmartClassroom-<version>-setup.exe`,
where `<version>` is the `version` field in `ui/package.json`.

The installer bundles the `smart-classroom\Windows` folder as the application. Local
state is left out: `models\`, `logs\`, `.proxy-config`, `.env`, `node_modules\`, and build
output are not packaged.

> [!NOTE]
> An installer you build yourself is not code-signed. Released installers are signed.

## Step 3: Install

Run the installer and complete the first-run setup as described in
[Get Started on Windows](./get-started-windows.md#step-2-install-the-app).

## Learn More

- [UI README](../../../Windows/ui/README.md): Running the desktop app in development
  mode, its architecture, and its data locations.
- [Flutter + RAG companion app](../../../../utils/flutter/README.md): A separate,
  lightweight Content Search demo that runs from a source checkout.
