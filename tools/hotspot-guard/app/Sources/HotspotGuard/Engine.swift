import Foundation

/// Bridge to the Python engine bundled in the app. Read-only calls run as the user.
/// Anything that changes the firewall runs as root through the standard macOS
/// administrator prompt, so the app itself never holds elevated rights.
enum Engine {
    static let python = "/usr/bin/python3"
    static let stateDirectory = "/var/db/hotspot-guard"
    static let watchLog = stateDirectory + "/watch.log"

    struct Output {
        let status: Int32
        let stdout: String
        let stderr: String
    }

    enum Failure: LocalizedError {
        case missingEngine
        case cancelled
        case command(String)

        var errorDescription: String? {
            switch self {
            case .missingEngine:
                return "The hotspot-guard engine is missing from the app bundle."
            case .cancelled:
                return "Cancelled."
            case .command(let message):
                let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
                return text.isEmpty ? "The engine failed without a message." : text
            }
        }
    }

    /// The allowlist the user edits. It lives in Application Support, so app updates never overwrite it.
    static func configURL() throws -> URL {
        let support = try FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        let directory = support.appendingPathComponent("HotspotGuard", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let config = directory.appendingPathComponent("hotspot-guard.ini")
        if !FileManager.default.fileExists(atPath: config.path),
           let bundled = Bundle.main.url(forResource: "hotspot-guard", withExtension: "ini", subdirectory: "engine") {
            try FileManager.default.copyItem(at: bundled, to: config)
        }
        return config
    }

    static func scriptURL() throws -> URL {
        guard let url = Bundle.main.url(forResource: "hotspot_guard", withExtension: "py", subdirectory: "engine") else {
            throw Failure.missingEngine
        }
        return url
    }

    /// Engine arguments with the user's config file. `-B` keeps Python from writing into the signed bundle.
    private static func engineArguments(_ args: [String]) throws -> [String] {
        ["-B", try scriptURL().path, "--config", try configURL().path] + args
    }

    private static func shellCommand(_ args: [String]) throws -> String {
        let parts = try [python] + engineArguments(args)
        return parts.map(shellQuote).joined(separator: " ")
    }

    /// Runs the engine as the user, with no prompt.
    static func unprivileged(_ args: [String]) async throws -> Output {
        try await run(python, try engineArguments(args))
    }

    /// Runs the engine as root. macOS asks for the administrator password.
    static func privileged(_ args: [String]) async throws -> String {
        try await runAsAdministrator(try shellCommand(args) + " 2>&1")
    }

    /// Starts `watch` as a detached root process. Its output goes to the watch log.
    /// `env` execs Python in place, so the recorded PID is the watcher's own, and the
    /// unbuffered output means the Activity panel is current rather than minutes behind.
    static func startWatcher() async throws {
        let command = try shellCommand(["watch"])
        _ = try await runAsAdministrator(
            "mkdir -p \(shellQuote(stateDirectory)) && nohup env PYTHONUNBUFFERED=1 \(command) >> \(shellQuote(watchLog)) 2>&1 < /dev/null & echo $!")
    }

    static func runAsAdministrator(_ shell: String) async throws -> String {
        let escaped = shell
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "do shell script \"\(escaped)\" with administrator privileges"
        let result = try await run("/usr/bin/osascript", ["-e", script])
        if result.status != 0 {
            if result.stderr.contains("-128") { throw Failure.cancelled }  // user dismissed the prompt
            throw Failure.command(result.stderr)
        }
        return result.stdout
    }

    static func shellQuote(_ text: String) -> String {
        "'" + text.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    static func run(_ executable: String, _ arguments: [String]) async throws -> Output {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Output, Error>) in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: executable)
                process.arguments = arguments
                let stdout = Pipe()
                let stderr = Pipe()
                process.standardOutput = stdout
                process.standardError = stderr
                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: error)
                    return
                }
                let outData = stdout.fileHandleForReading.readDataToEndOfFile()
                let errData = stderr.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                continuation.resume(returning: Output(
                    status: process.terminationStatus,
                    stdout: String(decoding: outData, as: UTF8.self),
                    stderr: String(decoding: errData, as: UTF8.self)))
            }
        }
    }
}
