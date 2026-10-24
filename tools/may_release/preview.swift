// A small silent preview: `preview <recording.mp4> <preview.mp4> <width>` writes the recording's video alone, scaled
// to <width> points wide (its height even, the aspect kept), as H.264 at a low bit rate. device_ios.py compiles this
// with the pinned Xcode's swiftc on the device runner, as renders.py compiles stitch.swift; it reads and writes only
// the paths it is given.
import AVFoundation
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 4, let width = Double(arguments[3]), width >= 16, width <= 2000 else { exit(2) }
let asset = AVURLAsset(url: URL(fileURLWithPath: arguments[1]))
guard let track = asset.tracks(withMediaType: .video).first else { exit(3) }
let shown = track.naturalSize.applying(track.preferredTransform)
guard abs(shown.width) > 0, abs(shown.height) > 0 else { exit(3) }
let height = max(2, (width * abs(shown.height) / abs(shown.width) / 2).rounded() * 2)
// The writer scales: its size is the preview's as shown, so the source's transform is applied before scaling.
let rotated = abs(track.preferredTransform.b) == 1
guard let reader = try? AVAssetReader(asset: asset),
      let writer = try? AVAssetWriter(outputURL: URL(fileURLWithPath: arguments[2]), fileType: .mp4) else { exit(4) }
let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: rotated ? height : width, AVVideoHeightKey: rotated ? width : height,
    AVVideoScalingModeKey: AVVideoScalingModeResizeAspect,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 400_000]])
input.transform = track.preferredTransform
input.expectsMediaDataInRealTime = false
guard reader.canAdd(output), writer.canAdd(input) else { exit(4) }
reader.add(output)
writer.add(input)
guard reader.startReading(), writer.startWriting(), let first = output.copyNextSampleBuffer() else { exit(5) }
writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(first))
var pending: CMSampleBuffer? = first
var appended = true
let done = DispatchSemaphore(value: 0)
input.requestMediaDataWhenReady(on: DispatchQueue(label: "preview")) {
    while input.isReadyForMoreMediaData {
        guard let sample = pending ?? output.copyNextSampleBuffer() else {
            input.markAsFinished()
            done.signal()
            return
        }
        pending = nil
        if !input.append(sample) {
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
