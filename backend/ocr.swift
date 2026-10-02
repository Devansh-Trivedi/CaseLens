import Foundation
import Vision
import AppKit

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// CPU inference also works when a background terminal has no GPU session.
request.usesCPUOnly = true
let handler = VNImageRequestHandler(url: url, options: [:])
do {
    try handler.perform([request])
    print((request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n"))
} catch {
    fputs("OCR failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
