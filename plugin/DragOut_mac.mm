/**
 * DragOut_mac.mm — macOS NSDraggingSession drag-out for LinkVST.
 *
 * Flow:
 *   1. Write MIDI bytes to a unique temp file.
 *   2. Wrap the file URL in an NSDraggingItem.
 *   3. Call -[NSView beginDraggingSessionWithItems:event:source:] using
 *      the current NSEvent from [NSApp currentEvent].
 *   4. NSDraggingSource delegate returns NSDragOperationCopy.
 *   5. On drag end, temp file is deleted.
 *
 * The platform_view ptr passed from LinkVST::BeginDragOut is the NSView*
 * returned by IGraphics::GetPlatformContext() in iPlug2 Cocoa builds.
 */

#ifdef OS_MAC

#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#include "LinkVST.h"

// ---------------------------------------------------------------------------
// NSDraggingSource implementation
// ---------------------------------------------------------------------------

@interface LVDragSource : NSObject <NSDraggingSource>
@property (nonatomic, copy) NSString* tempPath;
@end

@implementation LVDragSource

- (NSDragOperation)draggingSession:(NSDraggingSession*)session
    sourceOperationMaskForDraggingContext:(NSDraggingContext)context
{
    return NSDragOperationCopy;
}

- (void)draggingSession:(NSDraggingSession*)session
           endedAtPoint:(NSPoint)screenPoint
              operation:(NSDragOperation)operation
{
    // Clean up temp file after drag completes (copy or cancel)
    if (_tempPath) {
        [[NSFileManager defaultManager] removeItemAtPath:_tempPath error:nil];
    }
}

- (BOOL)ignoreModifierKeysForDraggingSession:(NSDraggingSession*)session {
    return YES;
}

@end

// ---------------------------------------------------------------------------
// LinkVST::DoDragOut (macOS)
// ---------------------------------------------------------------------------

void LinkVST::DoDragOut(const std::vector<uint8_t>& midi_bytes,
                         const std::string& filename,
                         void* platform_view,
                         float x, float y)
{
    NSView* view = (__bridge NSView*)platform_view;
    if (!view) return;

    // Write to a unique temp path to avoid collisions on rapid successive drags
    NSString* nsFilename = [NSString stringWithUTF8String:filename.c_str()];
    NSString* uuid       = [[NSUUID UUID] UUIDString];
    NSString* tmpPath    = [NSTemporaryDirectory()
                              stringByAppendingPathComponent:
                              [NSString stringWithFormat:@"linkvst_%@_%@", uuid, nsFilename]];

    NSData* data = [NSData dataWithBytes:midi_bytes.data() length:midi_bytes.size()];
    if (![data writeToFile:tmpPath atomically:YES]) {
        NSLog(@"LinkVST: failed to write drag temp file: %@", tmpPath);
        return;
    }

    NSURL* fileURL = [NSURL fileURLWithPath:tmpPath];

    // Dragging item — thumbnail is a generic MIDI icon (nil = system default)
    NSDraggingItem* dragItem = [[NSDraggingItem alloc] initWithPasteboardWriter:fileURL];

    // Place the drag image under the cursor (flip y: iPlug uses top-left origin)
    NSRect viewBounds = view.bounds;
    CGFloat flippedY  = viewBounds.size.height - y;
    NSRect dragFrame  = NSMakeRect(x - 16.0, flippedY - 16.0, 32.0, 32.0);

    // Provide a small visual drag image
    NSImage* dragImage = [[NSWorkspace sharedWorkspace] iconForFileType:@"mid"];
    dragImage.size = NSMakeSize(32, 32);
    [dragItem setDraggingFrame:dragFrame contents:dragImage];

    NSEvent* event = [NSApp currentEvent];
    if (!event) {
        // Fallback: synthesize a mouse event at the current cursor position
        NSPoint loc = [view convertPoint:[NSEvent mouseLocation] fromView:nil];
        event = [NSEvent mouseEventWithType:NSEventTypeLeftMouseDown
                                   location:loc
                              modifierFlags:0
                                  timestamp:NSProcessInfo.processInfo.systemUptime
                               windowNumber:view.window.windowNumber
                                    context:nil
                                eventNumber:0
                                 clickCount:1
                                   pressure:1.0];
    }

    LVDragSource* source = [[LVDragSource alloc] init];
    source.tempPath = tmpPath;

    [view beginDraggingSessionWithItems:@[dragItem] event:event source:source];
}

#endif // OS_MAC
