import AppKit

struct Startup: Decodable {
    let origin: String?
    let token: String?
    let error: String?
}

struct UsageRow: Decodable {
    let label: String
    let high: Bool

    init(from decoder: Decoder) throws {
        var values = try decoder.unkeyedContainer()
        label = try values.decode(String.self)
        high = try values.decode(Bool.self)
    }
}

struct Usage: Decodable {
    let rows: [UsageRow]?
    let credits: Int?
    let online: Bool?
}

struct SourceSession: Decodable {
    let id: String
    let provider: String
    let stamp: Double
    var name: String { provider == "claude" ? "Claude Code" : "Codex" }
    var destination: String { provider == "claude" ? "Codex" : "Claude Code" }
}

struct HandoffStatus: Decodable {
    let busy: Bool
    let message: String
}

struct RelayState: Decodable {
    let sources: [SourceSession]
    let handoff: HandoffStatus
    let codex: Usage
    let claude: [UsageRow]
    let detail: String
    let project: String
    let choices: [String]
    let automatic: Bool
    let saved: Double?
    let request: String
}

struct ActionReply: Decodable {
    let message: String?
}

final class RelayMenu: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let backend = Process()
    private let lifetime = Pipe()
    private var timer: Timer?
    private var service: Startup?
    private var state: RelayState?
    private var status = "Connecting…"
    private var fetching = false
    private var wasHigh = false
    private var starting = true
    private var warningPanel: NSPanel?
    private var previewChat = ProcessInfo.processInfo.environment["RELAY_PREVIEW_CODEX_CHAT"]
    private let previewFolder = ProcessInfo.processInfo.environment["RELAY_PREVIEW_FOLDER"]
    private var selectingPreview = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item.button?.title = "⇄ Relay"
        item.button?.setAccessibilityLabel("Relay")
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        rebuild()
        startBackend()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        item.button?.performClick(nil)
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        try? lifetime.fileHandleForWriting.close()
    }

    private func startBackend() {
        guard let script = Bundle.main.url(forResource: "popup", withExtension: "py") else {
            startupFailed("The Relay backend is missing from this app bundle.")
            return
        }
        let output = Pipe()
        let errors = Pipe()
        backend.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        backend.arguments = ["python3", "-B", script.path]
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (environment["PATH"] ?? "")
        backend.environment = environment
        backend.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
        backend.standardInput = lifetime
        backend.standardOutput = output
        backend.standardError = errors
        backend.terminationHandler = { [weak self] process in
            let data = errors.fileHandleForReading.readDataToEndOfFile()
            let message = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                if self?.service != nil || process.terminationStatus != 0 {
                    self?.startupFailed(message.isEmpty ? "The Relay backend exited (status \(process.terminationStatus))." : message)
                }
            }
        }
        do {
            try backend.run()
        } catch {
            startupFailed(error.localizedDescription)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var line = Data()
            let handle = output.fileHandleForReading
            while let byte = try? handle.read(upToCount: 1), !byte.isEmpty, byte.first != 10 {
                line.append(byte)
            }
            let startup = try? JSONDecoder().decode(Startup.self, from: line)
            DispatchQueue.main.async {
                guard let startup else { return }
                if let error = startup.error {
                    self?.startupFailed(error)
                } else {
                    self?.service = startup
                    self?.refresh()
                }
            }
        }
    }

    private func startupFailed(_ message: String) {
        service = nil
        state = nil
        status = message
        rebuild()
        if starting {
            starting = false
            _ = alert("Relay could not start", detail: message)
        }
    }

    private func call<T: Decodable>(_ path: String, body: [String: String]? = nil,
                                    completion: @escaping (Result<T, Error>) -> Void) {
        guard let origin = service?.origin, let token = service?.token,
              let url = URL(string: "\(origin)/\(token)/\(path)") else { return }
        var request = URLRequest(url: url, timeoutInterval: body == nil ? 3 : 35)
        if let body {
            request.httpMethod = "POST"
            request.setValue(origin, forHTTPHeaderField: "Origin")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONEncoder().encode(body)
        }
        URLSession.shared.dataTask(with: request) { data, _, error in
            let result = Result {
                if let error { throw error }
                return try JSONDecoder().decode(T.self, from: data ?? Data())
            }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    private func refresh() {
        guard service != nil, !fetching else { return }
        fetching = true
        call("state") { [weak self] (result: Result<RelayState, Error>) in
            guard let self else { return }
            self.fetching = false
            if case let .success(state) = result {
                self.state = state
                let high = (state.codex.online == true && state.codex.rows?.contains(where: { $0.high }) == true)
                    || state.claude.contains(where: { $0.high })
                self.item.button?.title = high ? "⇄ Relay · 90%+" : "⇄ Relay"
                if state.handoff.busy { self.item.button?.title = "⇄ Saving…" }
                let justReachedLimit = high && !self.wasHigh
                if justReachedLimit { NSSound.beep() }
                self.wasHigh = high
                if let chat = self.previewChat {
                    if state.project == self.previewFolder,
                       let source = state.sources.first(where: { $0.provider == "codex" && $0.id == chat }) {
                        self.starting = false
                        self.previewChat = nil
                        self.showWarning(project: state.project, provider: "codex", detail: "90% of the weekly window used", source: source, preview: true)
                    } else if let folder = self.previewFolder, state.project != folder,
                              state.choices.contains(folder), !self.selectingPreview {
                        self.selectingPreview = true
                        self.call("select", body: ["path": folder]) { (_: Result<ActionReply, Error>) in
                            self.selectingPreview = false
                            self.refresh()
                        }
                    }
                } else if justReachedLimit && !state.handoff.busy {
                    let codexRow = state.codex.online == true ? state.codex.rows?.first(where: { $0.high }) : nil
                    let provider = codexRow == nil ? "claude" : "codex"
                    let row = codexRow ?? state.claude.first(where: { $0.high })
                    self.starting = false
                    self.showWarning(project: state.project, provider: provider, detail: row?.label ?? "90% used", source: state.sources.first { $0.provider == provider }, preview: false)
                } else if self.starting {
                    self.starting = false
                    self.rebuild()
                    self.item.button?.performClick(nil)
                }
            } else {
                self.status = "Connecting…"
            }
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        rebuild()
        refresh()
    }

    private func label(_ title: String) {
        let row = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        row.isEnabled = false
        menu.addItem(row)
    }

    @discardableResult
    private func command(_ title: String, action: Selector, value: String? = nil) -> NSMenuItem {
        let row = NSMenuItem(title: title, action: action, keyEquivalent: "")
        row.target = self
        row.representedObject = value
        menu.addItem(row)
        return row
    }

    private func rebuild() {
        menu.removeAllItems()
        if let state {
            let project = state.project
            if project.isEmpty {
                label("Waiting for project activity")
            } else {
                let path = URL(fileURLWithPath: project)
                label(path.lastPathComponent)
                menu.items.last?.toolTip = project
                if state.choices.count > 1 || !state.automatic {
                    let picker = NSMenuItem(title: "Switch project", action: nil, keyEquivalent: "")
                    let options = NSMenu()
                    let automatic = NSMenuItem(title: "Follow active project", action: #selector(selectProject(_:)), keyEquivalent: "")
                    automatic.target = self
                    automatic.representedObject = ""
                    automatic.state = state.automatic ? .on : .off
                    options.addItem(automatic)
                    options.addItem(.separator())
                    for choice in state.choices {
                        let option = NSMenuItem(title: URL(fileURLWithPath: choice).lastPathComponent,
                                                action: #selector(selectProject(_:)), keyEquivalent: "")
                        option.target = self
                        option.representedObject = choice
                        option.toolTip = choice
                        option.state = !state.automatic && choice == project ? .on : .off
                        options.addItem(option)
                    }
                    picker.submenu = options
                    menu.addItem(picker)
                }
                if !state.handoff.message.isEmpty { label(state.handoff.message) }
                if !state.handoff.busy, let source = state.sources.first {
                    addHandoff(source, project: project, to: menu)
                    if state.sources.count > 1 {
                        let picker = NSMenuItem(title: "Hand off another session", action: nil, keyEquivalent: "")
                        let options = NSMenu()
                        for other in state.sources.dropFirst() {
                            addHandoff(other, project: project, to: options, detailed: true)
                        }
                        picker.submenu = options
                        menu.addItem(picker)
                    }
                }
                if state.sources.isEmpty { command("Copy checkpoint request", action: #selector(copyCheckpoint)) }
                if state.saved != nil {
                    command("Continue in Claude Code…", action: #selector(continueProject(_:)), value: "claude")
                    command("Continue in Codex…", action: #selector(continueProject(_:)), value: "codex")
                }
            }
            menu.addItem(.separator())
            label(state.codex.online == true ? "Codex" : state.codex.online == false ? "Codex · Disconnected" : "Codex · Connecting")
            for row in state.codex.rows ?? [] { label(row.label) }
            if let credits = state.codex.credits {
                label("Earned resets available · \(credits)")
                if state.codex.online == true && credits > 0 && state.codex.rows?.contains(where: { $0.high }) == true {
                    command("Use one Codex reset…", action: #selector(redeemReset))
                }
            }
            menu.addItem(.separator())
            label("Claude Code")
            for row in state.claude { label(row.label) }
            label(state.detail)
            menu.addItem(.separator())
            command("Refresh usage", action: #selector(refreshUsage))
        } else {
            label(status.components(separatedBy: .newlines).last ?? status)
        }
        command("Quit Relay", action: #selector(quit))
    }

    private func alert(_ title: String, detail: String = "", confirmation: Bool = false) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = detail
        alert.addButton(withTitle: confirmation ? "Continue" : "OK")
        if confirmation { alert.addButton(withTitle: "Cancel") }
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func action(_ route: String, _ body: [String: String], showReply: Bool = false) {
        call(route, body: body) { [weak self] (result: Result<ActionReply, Error>) in
            guard let self else { return }
            switch result {
            case let .success(reply):
                if showReply, let message = reply.message { _ = self.alert(message) }
            case let .failure(error):
                _ = self.alert("Relay action failed", detail: error.localizedDescription)
            }
            self.refresh()
        }
    }

    @objc private func selectProject(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        action("select", ["path": path])
    }

    private func showWarning(project: String, provider: String, detail: String, source: SourceSession?, preview: Bool) {
        warningPanel?.close()
        let panel = NSPanel(contentRect: .zero, styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView],
                            backing: .buffered, defer: false)
        panel.title = "Relay usage warning"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        let context = NSTextField(labelWithString: preview ? "⇄  Relay   ·   90% preview" : "⇄  Relay")
        context.textColor = .secondaryLabelColor
        stack.addArrangedSubview(context)
        let name = provider == "claude" ? "Claude Code" : "Codex"
        let heading = NSTextField(labelWithString: name + " is nearing its usage limit")
        heading.font = .systemFont(ofSize: 17, weight: .semibold)
        stack.addArrangedSubview(heading)
        let usage = NSTextField(labelWithString: detail)
        stack.addArrangedSubview(usage)
        if !project.isEmpty {
            let folder = NSTextField(labelWithString: "Project · " + URL(fileURLWithPath: project).lastPathComponent)
            folder.toolTip = project
            folder.lineBreakMode = .byTruncatingMiddle
            stack.addArrangedSubview(folder)
        }
        let buttons = NSStackView()
        buttons.orientation = .horizontal
        if let source {
            let button = NSButton(title: "Save and hand off to " + source.destination,
                                  target: self, action: #selector(handoffFromWarning(_:)))
            button.bezelStyle = .rounded
            button.bezelColor = .controlAccentColor
            button.identifier = NSUserInterfaceItemIdentifier("relay-save-handoff")
            button.toolTip = source.name + " · " + source.id
            warningTarget = ["path": project, "id": source.id, "provider": source.provider]
            buttons.addArrangedSubview(button)
        } else {
            warningTarget = nil
        }
        buttons.addArrangedSubview(NSButton(title: "Dismiss", target: self, action: #selector(dismissWarning)))
        stack.setCustomSpacing(14, after: stack.arrangedSubviews.last!)
        stack.addArrangedSubview(buttons)
        let padding: CGFloat = 18
        let size = stack.fittingSize
        panel.setContentSize(NSSize(width: max(330, size.width) + 2 * padding, height: size.height + 2 * padding))
        if let content = panel.contentView {
            content.addSubview(stack)
            stack.frame = content.bounds.insetBy(dx: padding, dy: padding)
        }
        let screen = NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) } ?? NSScreen.main
        if let frame = screen?.visibleFrame {
            panel.setFrameTopLeftPoint(NSPoint(x: frame.maxX - panel.frame.width - 14, y: frame.maxY - 10))
        }
        warningPanel = panel
        panel.orderFrontRegardless()
    }

    private var warningTarget: [String: String]?

    @objc private func handoffFromWarning(_ sender: NSButton) {
        guard let body = warningTarget else { return }
        dismissWarning()
        action("handoff", body, showReply: true)
    }

    @objc private func dismissWarning() {
        warningPanel?.close()
        warningPanel = nil
        warningTarget = nil
    }

    private func addHandoff(_ source: SourceSession, project: String, to menu: NSMenu, detailed: Bool = false) {
        var title = "Save and hand off to " + source.destination
        if detailed {
            let time = DateFormatter.localizedString(from: Date(timeIntervalSince1970: source.stamp), dateStyle: .short, timeStyle: .short)
            title = source.name + " · " + time + " · " + String(source.id.suffix(8))
        }
        let row = NSMenuItem(title: title, action: #selector(startHandoff(_:)), keyEquivalent: "")
        row.toolTip = source.name + " · " + source.id + " → " + source.destination
        row.representedObject = ["path": project, "id": source.id, "provider": source.provider]
        row.target = self
        menu.addItem(row)
    }

    @objc private func startHandoff(_ sender: NSMenuItem) {
        guard let body = sender.representedObject as? [String: String] else { return }
        action("handoff", body, showReply: true)
    }

    @objc private func copyCheckpoint() {
        guard let state else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(state.request, forType: .string)
    }

    @objc private func continueProject(_ sender: NSMenuItem) {
        guard let project = state?.project, let target = sender.representedObject as? String,
              alert("Continue from checkpoint?", detail: "Pause the source agent before opening \(project).", confirmation: true) else { return }
        action("launch", ["path": project, "action": target], showReply: true)
    }

    @objc private func refreshUsage() { action("action", ["action": "refresh"]) }

    @objc private func redeemReset() {
        if alert("Spend one earned Codex reset?", confirmation: true) {
            action("action", ["action": "reset"], showReply: true)
        }
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = RelayMenu()
app.delegate = delegate
app.run()
