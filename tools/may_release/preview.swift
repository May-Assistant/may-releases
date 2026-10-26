// A small silent preview: `preview <preview.mp4> <width> <seconds> <fps> <bitrate> <recording.mp4>...` writes the
// recordings' video alone, joined in the order given (a scenario's parts), scaled to <width> points wide (its height
// even, the aspect kept), as a timelapse of at most <seconds> at <fps> frames a second, H.264 at about <bitrate> bits a
// second (device_tests.PREVIEW_*). device_ios.py compiles this with the pinned Xcode's swiftc on the device runner, as
// renders.py compiles stitch.swift; it reads and writes only the paths it is given.
import AVFoundation
import Foundation

let arguments = CommandLine.arguments
guard arguments.count >= 7, let width = Double(arguments[2]), width >= 16, width <= 2000,
      let maxSeconds = Double(arguments[3]), maxSeconds >= 1, let fps = Double(arguments[4]), fps >= 1, fps <= 60,
      let bitrate = Int(arguments[5]), bitrate >= 10_000 else { exit(2) }
// The parts end to end in one composition track, shown as the first part is (its size and transform).
let asset = AVMutableComposition()
guard let track = asset.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)
else { exit(3) }
var natural = CGSize.zero
for (index, path) in arguments[6...].enumerated() {
    let part = AVURLAsset(url: URL(fileURLWithPath: path))
    guard let source = part.tracks(withMediaType: .video).first else { exit(3) }
    if index == 0 {
        natural = source.naturalSize
        track.preferredTransform = source.preferredTransform
    }
    do {
        try track.insertTimeRange(source.timeRange, of: source, at: asset.duration)
    } catch {
        exit(3)
    }
}
let shown = natural.applying(track.preferredTransform)
guard abs(shown.width) > 0, abs(shown.height) > 0 else { exit(3) }
let height = max(2, (width * abs(shown.height) / abs(shown.width) / 2).rounded() * 2)
// The writer scales: its size is the preview's as shown, so the source's transform is applied before scaling.
let rotated = abs(track.preferredTransform.b) == 1
guard let reader = try? AVAssetReader(asset: asset),
      let writer = try? AVAssetWriter(outputURL: URL(fileURLWithPath: arguments[1]), fileType: .mp4) else { exit(4) }
let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: rotated ? height : width, AVVideoHeightKey: rotated ? width : height,
    AVVideoScalingModeKey: AVVideoScalingModeResizeAspect,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: bitrate]])
input.transform = track.preferredTransform
input.expectsMediaDataInRealTime = false
guard reader.canAdd(output), writer.canAdd(input) else { exit(4) }
reader.add(output)
writer.add(input)
// The whole recording, sped up to last maxSeconds at most; a frame is kept only once its sped-up time reaches the next
// 1/fps slot, and is retimed to it.
let speed = max(1, CMTimeGetSeconds(asset.duration) / maxSeconds)
guard reader.startReading(), writer.startWriting(), let first = output.copyNextSampleBuffer() else { exit(5) }
let start = CMSampleBufferGetPresentationTimeStamp(first)
writer.startSession(atSourceTime: .zero)
var pending: CMSampleBuffer? = first
var appended = true
var slot = 0.0

func retimed(_ sample: CMSampleBuffer) -> CMSampleBuffer? {
    let at = CMTimeGetSeconds(CMTimeSubtract(CMSampleBufferGetPresentationTimeStamp(sample), start)) / speed
    guard at + 1e-6 >= slot, at <= maxSeconds else { return nil }
    slot = (at * fps).rounded(.down) / fps + 1 / fps
    var timing = CMSampleTimingInfo(duration: CMTime(seconds: 1 / fps, preferredTimescale: 600),
                                    presentationTimeStamp: CMTime(seconds: at, preferredTimescale: 600),
                                    decodeTimeStamp: .invalid)
    var copy: CMSampleBuffer?
    guard CMSampleBufferCreateCopyWithNewTiming(allocator: kCFAllocatorDefault, sampleBuffer: sample,
                                                sampleTimingEntryCount: 1, sampleTimingArray: &timing,
                                                sampleBufferOut: &copy) == noErr else { return nil }
    return copy
}
let done = DispatchSemaphore(value: 0)
input.requestMediaDataWhenReady(on: DispatchQueue(label: "preview")) {
    while input.isReadyForMoreMediaData {
        guard let sample = pending ?? output.copyNextSampleBuffer() else {
            input.markAsFinished()
            done.signal()
            return
        }
        pending = nil
        guard let kept = retimed(sample) else { continue }
        if !input.append(kept) {
            appended = false
            input.markAsFinished()
            done.signal()
            return
        }
    }
}
done.wait()
let finished = DispatchSemaphore(value: 0)
writer.finishWriting { finished.signal() }
finished.wait()
exit(appended && reader.status == .completed && writer.status == .completed ? 0 : 6)
