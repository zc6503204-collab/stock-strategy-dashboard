import Foundation
import AppKit
import UserNotifications

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let center = UNUserNotificationCenter.current()
let mode = CommandLine.arguments.dropFirst().first ?? "open"
func finish(_ status: String) {
    let data = try! JSONSerialization.data(withJSONObject: ["status":status])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}
func label(_ s: UNAuthorizationStatus) -> String {
    switch s {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .notDetermined: return "not_determined"
    case .provisional: return "provisional"
    default: return "unknown"
    }
}
if mode == "open" {
    NSWorkspace.shared.open(URL(string:"http://127.0.0.1:8765/")!)
    finish("opened")
} else if mode == "request" {
    center.requestAuthorization(options:[.alert,.sound,.badge]) { _,_ in
        center.getNotificationSettings { settings in finish(label(settings.authorizationStatus)) }
    }
} else if mode == "status" {
    center.getNotificationSettings { settings in finish(label(settings.authorizationStatus)) }
} else if mode == "send" {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let body = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]
    center.getNotificationSettings { settings in
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
            finish(label(settings.authorizationStatus))
            return
        }
        let content = UNMutableNotificationContent()
        content.title = body["title"] as? String ?? "短线观察台"
        content.body = body["message"] as? String ?? ""
        if body["sound"] as? Bool == true { content.sound = .default }
        let request = UNNotificationRequest(identifier:body["id"] as? String ?? UUID().uuidString,content:content,trigger:nil)
        center.add(request) { error in finish(error == nil ? "submitted" : "error") }
    }
} else { finish("unknown_mode") }
RunLoop.main.run(until:Date().addingTimeInterval(10))
finish("timeout")
