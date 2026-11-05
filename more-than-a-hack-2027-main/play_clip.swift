import Cocoa
import AVFoundation
import AVKit

let args = CommandLine.arguments
guard args.count == 4,
      let start = Double(args[2]),
      let end = Double(args[3]),
      end > start else {
    fputs("usage: play_clip.swift PATH START_SECONDS END_SECONDS\n", stderr)
    exit(2)
}

final class ClipPlayer: NSObject {
    let player = AVPlayer()
    let startSeconds: Double
    let endSeconds: Double
    var endObserver: Any?

    init(url: URL, start: Double, end: Double) {
        startSeconds = start
        endSeconds = end
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
            self.player.play()
            let endTime = NSValue(time: CMTime(seconds: self.endSeconds, preferredTimescale: 25))
            self.endObserver = self.player.addBoundaryTimeObserver(
                forTimes: [endTime], queue: .main) { [weak self] in
                self?.player.pause()
            }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let url = URL(fileURLWithPath: args[1])
let box = ClipPlayer(url: url, start: start, end: end)

let view = AVPlayerView(frame: NSRect(x: 0, y: 0, width: 1280, height: 720))
view.controlsStyle = .floating
view.player = box.player

let window = NSWindow(
    contentRect: NSRect(x: 0, y: 0, width: 1280, height: 720),
    styleMask: [.titled, .closable, .miniaturizable, .resizable],
    backing: .buffered,
    defer: false)
window.title = String(format: "tactical  %.2f s – %.2f s", start, end)
window.contentView = view
window.center()
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)
app.run()
