// Side by side: `stitch <left.png> <right.png> <out.png>` writes one PNG with the left image (Android) and the right
// image (iOS) next to each other, top-aligned, on white, 48 points apart. renders.py compiles this with the pinned
// Xcode's swiftc on the render runner; it reads and writes only the paths it is given.
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

func load(_ path: String) -> CGImage? {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(source, 0, nil)
}

let arguments = CommandLine.arguments
guard arguments.count == 4, let left = load(arguments[1]), let right = load(arguments[2]) else { exit(2) }
let gap = 48
let width = left.width + gap + right.width
let height = max(left.height, right.height)
guard width <= 30000, height <= 15000,
      let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpace(name: CGColorSpace.sRGB)!,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { exit(3) }
context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
context.fill(CGRect(x: 0, y: 0, width: width, height: height))
// CoreGraphics' origin is bottom left: top-align each image.
context.draw(left, in: CGRect(x: 0, y: height - left.height, width: left.width, height: left.height))
context.draw(right, in: CGRect(x: left.width + gap, y: height - right.height, width: right.width, height: right.height))
guard let image = context.makeImage(),
      let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: arguments[3]) as CFURL,
                                                        UTType.png.identifier as CFString, 1, nil) else { exit(4) }
CGImageDestinationAddImage(destination, image, nil)
exit(CGImageDestinationFinalize(destination) ? 0 : 5)
