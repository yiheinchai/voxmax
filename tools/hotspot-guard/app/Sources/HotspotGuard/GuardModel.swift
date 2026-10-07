import Foundation
import SwiftUI

struct AllowGroup: Identifiable, Decodable, Equatable {
    let name: String
    let description: String
    let enabled: Bool
    let domainCount: Int
    var id: String { name }
}

/// What the engine recorded (`status --json`). It is read without root, so it cannot see the firewall itself.
struct BlockStatus: Decodable, Equatable {
    let active: Bool
    let watching: Bool
    let watcherPid: Int?
    let appliedAt: Double?
    let remembered: Int

    static let off = BlockStatus(active: false, watching: false, watcherPid: nil, appliedAt: nil, remembered: 0)
}

struct ResolvedAddress: Identifiable, Decodable, Equatable {
    let host: String
    let ip: String
    var id: String { host + " " + ip }
}

struct PlanResult: Decodable, Equatable {
    let addresses: [ResolvedAddress]
    let failed: [String]
    let networks: Int
}

enum BlockState: Equatable {
    case off
    case on
    /// Rules are recorded as active but no watcher is running, for example after a reboot.
    case stale
}

@MainActor
final class GuardModel: ObservableObject {
    @Published private(set) var groups: [AllowGroup] = []
    @Published private(set) var status: BlockStatus = .off
    @Published private(set) var logLines: [String] = []
    @Published private(set) var plan: PlanResult?
    @Published private(set) var busy = false
    @Published private(set) var pendingChanges = false
    @Published var showingPlan = false
    /// Set by a user action. Shown as an alert once.
    @Published var errorMessage: String?
    /// Set when background polling cannot read the engine. Shown as a banner, not an alert, so it never repeats.
    @Published private(set) var engineProblem: String?

    var blockState: BlockState {
        guard status.active else { return .off }
        return status.watching ? .on : .stale
    }

    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    /// Polled every few seconds. Reads only; never prompts.
    func refresh() async {
        do {
            status = try await json(BlockStatus.self, ["status", "--json"])
            groups = try await json([AllowGroup].self, ["groups", "--json"])
            logLines = tailLog()
            engineProblem = nil
            if blockState != .on { pendingChanges = false }
        } catch {
            engineProblem = error.localizedDescription
        }
    }

    func setGroup(_ name: String, enabled: Bool) async {
        await perform {
            let output = try await Engine.unprivileged(["set-group", name, enabled ? "on" : "off"])
            guard output.status == 0 else { throw Engine.Failure.command(output.stderr) }
            if self.blockState == .on { self.pendingChanges = true }
        }
    }

    func startBlocking() async {
        await perform {
            try await Engine.startWatcher()
            try await Task.sleep(for: .seconds(2))  // give the watcher time to install the rules
        }
    }

    func stopBlocking() async {
        await perform {
            _ = try await Engine.privileged(["disable"])
            self.pendingChanges = false
        }
    }

    /// Re-resolves the allowlist into the live rules now, instead of waiting for the next refresh cycle.
    func applyChanges() async {
        await perform {
            _ = try await Engine.privileged(["refresh"])
            self.pendingChanges = false
        }
    }

    func loadPlan() async {
        await perform {
            self.plan = try await self.json(PlanResult.self, ["plan", "--json"])
            self.showingPlan = true
        }
    }

    private func perform(_ action: () async throws -> Void) async {
        busy = true
        defer { busy = false }
        do {
            try await action()
        } catch Engine.Failure.cancelled {
            // The user dismissed the password prompt. Nothing to report.
        } catch {
            errorMessage = error.localizedDescription
        }
        await refresh()
    }

    private func json<T: Decodable>(_ type: T.Type, _ args: [String]) async throws -> T {
        let output = try await Engine.unprivileged(args)
        guard output.status == 0 else { throw Engine.Failure.command(output.stderr) }
        return try decoder.decode(type, from: Data(output.stdout.utf8))
    }

    private func tailLog() -> [String] {
        guard let text = try? String(contentsOfFile: Engine.watchLog, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").suffix(40).map(String.init)
    }
}
