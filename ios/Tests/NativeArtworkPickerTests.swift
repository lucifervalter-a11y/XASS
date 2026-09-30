import XCTest
import UIKit
import ImageIO
@testable import XASS

final class NativeArtworkPickerTests: XCTestCase {
    @MainActor func testArtworkIsDownsampledAndEncodedWithinUploadLimit() throws {
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: 2400, height: 1600))
        let source = renderer.image { context in
            UIColor(red: 0.12, green: 0.28, blue: 0.72, alpha: 1).setFill()
            context.fill(CGRect(x: 0, y: 0, width: 2400, height: 1600))
        }.pngData()!
        let jpeg = try NativeArtworkUploadPolicy.normalizedJPEG(source)
        XCTAssertLessThanOrEqual(jpeg.count, NativeArtworkUploadPolicy.maximumUploadBytes)
        let imageSource = try XCTUnwrap(CGImageSourceCreateWithData(jpeg as CFData, nil))
        let properties = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(imageSource, 0, nil) as? [CFString: Any])
        let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue ?? 0
        let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue ?? 0
        XCTAssertLessThanOrEqual(max(width, height), NativeArtworkUploadPolicy.maximumPixelSize)
    }

    func testArtworkPolicyRejectsNonImageAndOversizedInput() {
        XCTAssertThrowsError(try NativeArtworkUploadPolicy.normalizedJPEG(Data("not an image".utf8)))
        XCTAssertThrowsError(try NativeArtworkUploadPolicy.normalizedJPEG(
            Data(repeating: 0, count: NativeArtworkUploadPolicy.maximumSourceBytes + 1)))
    }
}
