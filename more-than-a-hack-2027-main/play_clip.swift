import Cocoa
import AVFoundation
import AVKit

let args = CommandLine.arguments
let pauseArg = args.count == 5 ? Double(args[4]) : nil
guard args.count == 4 || args.count == 5,
      let start = Double(args[2]),
      let end = Double(args[3]),
      end > start,
      pauseArg == nil || (pauseArg! >= start && pauseArg! < end) else {
    fputs("usage: play_clip.swift PATH START_SECONDS END_SECONDS [PAUSE_SECONDS]\n", stderr)
    exit(2)
}

final class ClipPlayer: NSObject {
    let player = AVPlayer()
    let startSeconds: Double
    let endSeconds: Double
    let holdSeconds: Double?
    let frame = 1.0 / 25.0
    var tick: Any?
    var held = false

    init(url: URL, start: Double, end: Double, hold: Double?) {
        startSeconds = start
        endSeconds = end
        holdSeconds = hold
        super.init()
        let item = AVPlayerItem(url: url)
        player.replaceCurrentItem(with: item)
        NotificationCenter.default.addObserver(
            forName: .AVPlayerItemFailedToPlayToEndTime, object: item, queue: .main) { note in
            fputs("playback failed: \(note)\n", stderr)
        }
        item.addObserver(self, forKeyPath: "status", options: [.initial, .new], context: nil)
    }

    override func observeValue(forKeyPath keyPath: String?, of object: Any?,
                                change: [NSKeyValueChangeKey: Any]?,
                                context: UnsafeMutableRawPointer?) {
        guard let item = player.currentItem else { return }
        if item.status == .failed {
            fputs("could not read the video: \(item.error?.localizedDescription ?? "unknown")\n", stderr)
            exit(1)
        }
        guard item.status == .readyToPlay else { return }
        item.removeObserver(self, forKeyPath: "status")
        let startTime = CMTime(seconds: startSeconds, preferredTimescale: 25)
        player.seek(to: startTime, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] finished in
            guard let self = self, finished else { return }
            self.watch()
            self.player.play()
        }
    }

    func watch() {
        let interval = CMTime(value: 1, timescale: 25)
        tick = player.addPeriodicTimeObserver(forInterval: interval, queue: .main) { [weak self] time in
            self?.onTick(time.seconds)
        }
    }

    func onTick(_ now: Double) {
        guard player.rate > 0 else { return }
        if let hold = holdSeconds, !held, now >= hold - 0.0001 {
            held = true
            freeze(at: hold)
            return
        }
        if now >= endSeconds - frame {
            freeze(at: endSeconds - frame)
        }
    }

    func freeze(at seconds: Double) {
        player.pause()
        let exact = CMTime(seconds: seconds, preferredTimescale: 25)
        player.seek(to: exact, toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func toggle() {
        if player.rate == 0 { player.play() } else { player.pause() }
    }

    func step(by frames: Int) {
        player.pause()
        let now = player.currentTime().seconds
        let target = min(max(now + Double(frames) * frame, startSeconds), endSeconds - frame)
        if let hold = holdSeconds, target < hold { held = false }
        player.seek(to: CMTime(seconds: target, preferredTimescale: 25),
                    toleranceBefore: .zero, toleranceAfter: .zero)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let url = URL(fileURLWithPath: args[1])
let box = ClipPlayer(url: url, start: start, end: end, hold: pauseArg)

let view = AVPlayerView(frame: NSRect(x: 0, y: 0, width: 1280, height: 720))
view.controlsStyle = .floating
view.player = box.player

let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: 1280, height: 720),
    styleMask: [.titled, .closable, .miniaturizable, .resizable],
    backing: .buffered,
    defer: false)
if let hold = pauseArg {
    window.title = String(format: "tactical  %.2f s – %.2f s, pauses at %.2f s", start, end, hold)
} else {
    window.title = String(format: "tactical  %.2f s – %.2f s", start, end)
}
window.contentView = view
window.center()
window.makeKeyAndOrderFront(nil)
NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
    switch event.keyCode {
    case 49:
        box.toggle()
        return nil
    case 123:
        box.step(by: -1)
        return nil
    case 124:
        box.step(by: 1)
        return nil
    default:
        return event
    }
}
app.activate(ignoringOtherApps: true)
app.run()
