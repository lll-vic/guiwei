#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

// Local-only image reading; no original-file mutations.
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 2) return 1;
        NSURL *url = [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[1]]];
        VNRecognizeTextRequest *request = [VNRecognizeTextRequest new];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.recognitionLanguages = @[@"zh-Hans", @"en-US"];
        request.usesLanguageCorrection = YES;
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithURL:url options:@{}];
        NSError *error = nil;
        if (![handler performRequests:@[request] error:&error]) return 2;
        NSMutableArray *lines = [NSMutableArray new];
        for (VNRecognizedTextObservation *observation in request.results) {
            VNRecognizedText *text = [[observation topCandidates:1] firstObject];
            if (text.string) [lines addObject:text.string];
        }
        NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"text": [lines componentsJoinedByString:@"\n"]} options:0 error:&error];
        if (!data) return 3;
        [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
    }
    return 0;
}
