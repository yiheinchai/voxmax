import SwiftUI

// MARK: - Liquid Glass building blocks

extension View {
    /// A Liquid Glass panel. Glass refracts whatever is behind it, so it sits over AmbientBackground.
    func glassPanel(tint: Color? = nil) -> some View {
        let glass: Glass = tint.map { Glass.regular.tint($0) } ?? .regular
        return self
            .padding(20)
            .glassEffect(glass, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
    }
}

/// Soft colour behind the content, so the glass has something to refract.
struct AmbientBackground: View {
    var body: some View {
        LinearGradient(
            colors: [.teal.opacity(0.45), .indigo.opacity(0.35), .orange.opacity(0.25)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
        .overlay(Color(nsColor: .windowBackgroundColor).opacity(0.35))
        .ignoresSafeArea()
    }
}

// MARK: - Window

struct ContentView: View {
    @StateObject private var model = GuardModel()

    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                if let problem = model.engineProblem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .font(.callout)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .glassPanel(tint: .orange)
                }
                StatusPanel(model: model)
                GroupsPanel(model: model)
                ActivityPanel(lines: model.logLines)
            }
            .padding(24)
        }
        .background { AmbientBackground() }
        .toolbar {
            ToolbarItemGroup {
                Button {
                    Task { await model.loadPlan() }
                } label: {
                    Label("Preview", systemImage: "list.bullet.rectangle")
                }
                .help("Resolve the allowlist and show the addresses it allows")
                .disabled(model.busy)

                Button(action: openAllowlist) {
                    Label("Edit Allowlist", systemImage: "doc.text")
                }
                .help("Open hotspot-guard.ini in your text editor")
            }
        }
        .sheet(isPresented: $model.showingPlan) {
            PlanSheet(model: model)
        }
        .alert("Hotspot Guard", isPresented: alertBinding) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.errorMessage ?? "")
        }
        .task { await poll() }
    }

    private var alertBinding: Binding<Bool> {
        Binding(
            get: { model.errorMessage != nil },
            set: { if !$0 { model.errorMessage = nil } }
        )
    }

    private func openAllowlist() {
        guard let url = try? Engine.configURL() else { return }
        NSWorkspace.shared.open(url)
    }

    private func poll() async {
        while !Task.isCancelled {
            await model.refresh()
            try? await Task.sleep(for: .seconds(4))
        }
    }
}

// MARK: - Status

struct StatusPanel: View {
    @ObservedObject var model: GuardModel

    private var title: String {
        switch model.blockState {
        case .off: return "Blocking is off"
        case .on: return "Blocking is on"
        case .stale: return "Rules may still be active"
        }
    }

    private var detail: String {
        switch model.blockState {
        case .off:
            return "Everything can use the hotspot. Turn blocking on to allow only the enabled groups."
        case .on:
            return "Only the enabled groups can connect. Their addresses refresh automatically."
        case .stale:
            return "The watcher is not running. Clear the rules to restore normal networking."
        }
    }

    private var symbol: String {
        switch model.blockState {
        case .off: return "shield.slash"
        case .on: return "checkmark.shield.fill"
        case .stale: return "exclamationmark.shield.fill"
        }
    }

    private var accent: Color? {
        switch model.blockState {
        case .off: return nil
        case .on: return .green
        case .stale: return .orange
        }
    }

    private var appliedText: String {
        guard let applied = model.status.appliedAt else { return "Never" }
        return Date(timeIntervalSince1970: applied).formatted(date: .omitted, time: .shortened)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(title).font(.title.weight(.semibold))
                    Text(detail).font(.callout).foregroundStyle(.secondary)
                }
                Spacer(minLength: 12)
                Image(systemName: symbol)
                    .font(.system(size: 40))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(accent ?? Color.secondary)
            }

            HStack(spacing: 12) {
                StatBadge(label: "Addresses", value: "\(model.status.remembered)")
                StatBadge(label: "Last applied", value: appliedText)
            }

            GlassEffectContainer(spacing: 12) {
                HStack(spacing: 12) {
                    actionButtons
                }
            }
            .disabled(model.busy)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassPanel(tint: accent)
    }

    @ViewBuilder
    private var actionButtons: some View {
        switch model.blockState {
        case .off:
            Button("Turn On Blocking") {
                Task { await model.startBlocking() }
            }
            .buttonStyle(.glassProminent)
            .tint(.green)
            .controlSize(.large)

        case .on:
            if model.pendingChanges {
                Button("Apply Changes") {
                    Task { await model.applyChanges() }
                }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
            }
            Button("Turn Off Blocking") {
                Task { await model.stopBlocking() }
            }
            .buttonStyle(.glass)
            .controlSize(.large)

        case .stale:
            Button("Clear Rules") {
                Task { await model.stopBlocking() }
            }
            .buttonStyle(.glassProminent)
            .tint(.orange)
            .controlSize(.large)
        }
    }
}

struct StatBadge: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.headline.monospacedDigit())
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.quaternary, in: Capsule())
    }
}

// MARK: - Groups

struct GroupsPanel: View {
    @ObservedObject var model: GuardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Allowed groups").font(.title3.weight(.semibold))
            if model.groups.isEmpty {
                Text("Loading…").foregroundStyle(.secondary)
            }
            ForEach(model.groups) { group in
                GroupRow(group: group, model: model)
                if group != model.groups.last {
                    Divider()
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassPanel()
    }
}

struct GroupRow: View {
    let group: AllowGroup
    @ObservedObject var model: GuardModel

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(group.name.capitalized).font(.body.weight(.medium))
                Text(group.description).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            Text("\(group.domainCount)")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            Toggle(group.name, isOn: Binding(
                get: { group.enabled },
                set: { value in Task { await model.setGroup(group.name, enabled: value) } }
            ))
            .toggleStyle(.switch)
            .labelsHidden()
            .disabled(model.busy)
        }
    }
}

// MARK: - Activity

struct ActivityPanel: View {
    let lines: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Activity").font(.title3.weight(.semibold))
            ScrollView {
                Text(lines.isEmpty ? "No activity yet. Turn blocking on to start the watcher." : lines.joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(lines.isEmpty ? Color.secondary : Color.primary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(height: 170)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassPanel()
    }
}

// MARK: - Preview sheet

struct PlanSheet: View {
    @ObservedObject var model: GuardModel
    @Environment(\.dismiss) private var dismiss

    private var summary: String {
        let addresses = model.plan?.addresses.count ?? 0
        let networks = model.plan?.networks ?? 0
        return "\(addresses) addresses resolved, \(networks) networks would be allowed"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Resolved addresses").font(.title2.weight(.semibold))
                Text(summary).font(.callout).foregroundStyle(.secondary)
            }

            List(model.plan?.addresses ?? []) { row in
                HStack {
                    Text(row.host)
                    Spacer()
                    Text(row.ip)
                        .font(.body.monospaced())
                        .foregroundStyle(.secondary)
                }
            }
            .listStyle(.inset)

            if let failed = model.plan?.failed, !failed.isEmpty {
                Text("No address, so still blocked: " + failed.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack {
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.glassProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 540, height: 520)
    }
}
